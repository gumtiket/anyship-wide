"""서비스 서버의 진짜 도커에서 ImageBuilder를 시험하는 수동 시험(자동 테스트가 아니다). AWS에는 아무것도 하지 않는다.

서비스 서버에서 실행한다. 확인하는 것:
  1) B의 샘플(AI/samples/todo)에 changes.diff와 Dockerfile을 적용한 변환본을 `<앱>:<SHA>`로 빌드한다(콜드와 캐시 빌드 시간)
  2) 이미지가 linux/amd64이고, 소스에 심어 둔 `.git`과 `.env*`가 이미지 안에 없다
     (B의 .dockerignore를 일부러 지워서, 빌더의 제외 로직만으로 막히는지 본다)
  3) 빌드 중 RUN 단계가 인스턴스 메타데이터(IAM 자격 증명)에 닿지 못한다  ← 가장 중요한 위험 확인
     (참고로 서비스 포트에 닿는지도 보고한다. 합격 조건은 아니다)
  4) prune이 진짜 도커의 출력(CreatedAt 형식)으로 동작한다

    python -u infra/adapters/scripts/smoke_image_builder.py

--keep-image를 주면 시험용 이미지(smoke-todo:*, smoke-imds-probe:*)를 지우지 않는다. 베이스 이미지(python:3.12-slim,
public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0)는 미리 받아 두는 것이 좋다(없으면 첫 빌드에 내려받아 오래 걸린다).
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import smoke_onprem as base
from smoke_onprem import expect, timed

from anyship_adapters.image_builder import BuildError, ImageBuilder

APP, SHA = "smoke-todo", "abcdef1"
PROBE_APP = "smoke-imds-probe"
ROOT = Path(__file__).resolve().parents[3]
PROBE = textwrap.dedent("""\
    import sys
    import urllib.error
    import urllib.request as u


    def reach(request):
        try:
            with u.urlopen(request, timeout=3):
                return True
        except urllib.error.HTTPError:
            return True  # 응답을 받았으니 닿은 것이다
        except Exception:
            return False


    token = u.Request("http://169.254.169.254/latest/api/token", method="PUT",
                      headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
    metadata, service = reach(token), reach("%s")
    print("probe: metadata_reachable =", metadata)
    print("probe: service_reachable =", service)
    sys.exit(1 if metadata else 0)  # 메타데이터에 닿으면 빌드를 실패시킨다
    """)


def docker(*args, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def assemble(sample: Path, out: Path, target: Path, *, plant: bool = True) -> None:
    """원본 샘플에 changes.diff와 Dockerfile을 적용한 변환본을 만든다.

    plant=True(기본)는 빌더의 제외 로직만 시험하려고 B의 .dockerignore를 지우고 비밀로 보이는 파일을 일부러 심는다.
    plant=False는 B가 만든 그대로(.dockerignore 포함, 심은 파일 없음)의 변환본이다."""
    shutil.copytree(sample, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv"))
    subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(out / "changes.diff")], cwd=target, check=True
    )
    shutil.copy(out / "Dockerfile", target / "Dockerfile")
    if not plant:
        return
    (target / ".dockerignore").unlink(
        missing_ok=True
    )  # B의 제외 규칙에 기대지 않고 빌더의 제외만 시험한다
    (target / ".git").mkdir()
    (target / ".git" / "config").write_text("[remote] url = https://planted.example/secret\n")
    for name in (".env", ".env.production"):
        (target / name).write_text("PLANTED_SECRET=should-never-reach-the-image\n")


def make_log(verbose: bool, lines: list):
    def log(event):
        lines.append(event.message)
        if (
            verbose
            or event.step == 1
            or "ERROR" in event.message
            or event.message.startswith("이미지")
        ):
            print(f"    {event.message[:140]}")

    return log


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default=str(ROOT / "AI" / "samples" / "todo"))
    parser.add_argument("--out", default=str(ROOT / "AI" / "ai" / "demo-cache" / "todo" / "out"))
    parser.add_argument(
        "--service-url",
        default="http://172.17.0.1:8000/api/health",
        help="빌드 컨테이너가 닿는지 보고할 서비스 주소",
    )
    parser.add_argument("--keep-image", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="빌드 출력 줄을 모두 보여 준다")
    args = parser.parse_args()
    builder, lines, image = ImageBuilder(timeout=900), [], f"{APP}:{SHA}"
    log = make_log(args.verbose, lines)
    work = Path(tempfile.mkdtemp(prefix="anyship-smoke-build-"))
    try:
        source = work / "source"
        assemble(Path(args.sample), Path(args.out), source)
        print(
            f"변환본을 조립했습니다: {sum(1 for p in source.rglob('*') if p.is_file())}개 파일(비밀로 보이는 파일 3개를 심어 둠)"
        )

        print("1) 빌드(콜드: 캐시를 쓸 수 있으면 쓴다)")
        built = timed(lambda: builder.build(source, APP, SHA, log))
        expect(
            "이미지 이름이 <앱>:<SHA>이다",
            built.image == image and built.image_id.startswith("sha256:"),
        )

        print("2) 같은 소스를 다시 빌드(캐시)")
        again = timed(lambda: builder.build(source, APP, SHA, lambda e: None))
        expect(
            "같은 소스를 다시 빌드해도 같은 이미지 ID다(캐시 재사용)",
            again.image_id == built.image_id,
        )

        print("3) 이미지 검사")
        info = docker("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image)
        expect("이미지가 linux/amd64다", info.stdout.strip() == "linux/amd64")
        found = docker(
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            image,
            "-c",
            "ls -A /app; test ! -e /app/.git && test ! -e /app/.env && test ! -e /app/.env.production",
        )
        expect(
            "소스에 심은 .git과 .env*가 이미지 안에 없다(.dockerignore 없이도)",
            found.returncode == 0,
        )
        expect(
            "앱 코드는 들어 있다(/app/app/main.py)",
            docker(
                "run", "--rm", "--entrypoint", "test", image, "-f", "/app/app/main.py"
            ).returncode
            == 0,
        )

        print("4) 빌드 중 RUN 단계가 인스턴스 메타데이터에 닿는지")
        probe = work / "probe"
        probe.mkdir()
        (probe / "probe.py").write_text(PROBE % args.service_url)
        (probe / "Dockerfile").write_text(
            "FROM python:3.12-slim\nCOPY probe.py /probe.py\nRUN python /probe.py\n"
        )
        probe_lines: list = []
        try:
            timed(
                lambda: builder.build(
                    probe, PROBE_APP, SHA, lambda e: probe_lines.append(e.message)
                )
            )
            blocked = True
        except BuildError as exc:
            blocked = False
            print(f"   -> 빌드 실패 {exc.error.code}: {exc.tail[-300:]}")
        expect("빌드 중에는 인스턴스 메타데이터에 닿지 못한다(닿으면 빌드가 실패한다)", blocked)
        reported = " ".join(probe_lines)
        service = re.search(r"service_reachable = (True|False)", reported)
        print(
            f"   참고: 빌드 컨테이너에서 서비스 포트에 닿는가 = {service.group(1) if service else '알 수 없음'}"
        )

        print("5) prune (진짜 docker image ls 출력으로)")
        for tag in ("1111111", "2222222"):
            docker("tag", image, f"{APP}:{tag}")
        removed = builder.prune(APP, keep=1)
        left = [t for t in docker("image", "ls", APP, "--format", "{{.Tag}}").stdout.split() if t]
        expect("3개 중 최근 1개만 남기고 2개를 지운다", len(removed) == 2 and len(left) == 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep_image:
            for name in (APP, PROBE_APP):
                tags = [
                    t
                    for t in docker(
                        "image", "ls", name, "--format", "{{.Repository}}:{{.Tag}}"
                    ).stdout.split()
                    if t
                ]
                if tags:
                    docker("image", "rm", *tags)

    failed = [label for label, passed in base.results if not passed]
    print(f"\n결과: {len(base.results) - len(failed)}/{len(base.results)} OK")
    for label in failed:
        print("  FAIL:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

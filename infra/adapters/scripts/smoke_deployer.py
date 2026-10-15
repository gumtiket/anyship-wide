"""배포 파이프라인(Deployer)을 한 번의 호출로 실제 AWS 호스트까지 돌리는 수동 시험.

서비스 서버에서 실행한다. 공용 기반이 이미 켜져 있어야 하고, `*.<환경ID>.aws.<도메인>` DNS 레코드가 있어야 한다.
기반이 없으면 20분짜리 apply를 시작하지 않고 멈춘다(이 시험은 기반을 만들지 않는다. apply를 막는 실행기를 쓴다).

  1) 잘못된 입력은 빌드 전에 거절된다(비밀 누락, 세트 불일치)
  2) Deployer.deploy 한 번: 빌드 -> 기반 확인 -> check -> 배포 -> 정리
  3) 로그가 큰 단계 1/5 ~ 5/5로 이어지고, 결과에 기반 값이 실린다
  4) 공개 주소에서 /healthz, 목록, 쓰기
  5) destroy (앱 DB `app_todo`는 남는다)

    EXTERNAL_ID=... python -u infra/adapters/scripts/smoke_deployer.py \\
      --role-arn <역할 ARN> --state-bucket <StateBucketName> --staging

--keep을 주면 마지막에 destroy하지 않고 앱을 남긴다.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import smoke_onprem as base
import yaml
from smoke_image_builder import assemble, docker
from smoke_onprem import expect, log, show, timed
from smoke_sample_deploy import request

from anyship_adapters import AwsEnvironment
from anyship_adapters.aws_always_on import AwsAlwaysOnAdapter
from anyship_adapters.deployer import Deployer
from anyship_adapters.foundation import FoundationSettings
from anyship_adapters.image_builder import ImageBuilder
from anyship_adapters.terraform_runner import TerraformRunner

ROOT = Path(__file__).resolve().parents[3]
SHA = "b2b2b2b"


class ReadOnlyRunner:
    """read_foundation만 진짜로 하고, apply는 막는다(기반이 없을 때 20분짜리 작업이 시작되지 않게)."""

    def __init__(self, runner: TerraformRunner):
        self._runner = runner

    def read_foundation(self, env, log):
        return self._runner.read_foundation(env, log)

    def apply(self, env, variables, log):
        raise RuntimeError("이 시험은 기반을 만들지 않습니다. 기반이 먼저 있어야 합니다.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--state-bucket", required=True)
    parser.add_argument("--env-id", default="test")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--key", default=str(Path.home() / ".ssh" / "onprem_deploy"))
    parser.add_argument("--domain", default="anyship.cloud")
    parser.add_argument("--sample", default=str(ROOT / "AI" / "samples" / "todo"))
    parser.add_argument("--out", default=str(ROOT / "AI" / "ai" / "demo-cache" / "todo" / "out"))
    parser.add_argument(
        "--plugin-cache", default=str(Path.home() / ".terraform.d" / "plugin-cache")
    )
    parser.add_argument(
        "--staging", action="store_true", help="Let's Encrypt staging 인증서면 TLS 검증을 끈다"
    )
    parser.add_argument("--keep", action="store_true", help="마지막에 destroy하지 않고 앱을 남긴다")
    args = parser.parse_args()
    external_id = os.environ.get("EXTERNAL_ID")
    if not external_id:
        print("EXTERNAL_ID 환경변수를 지정하세요(명령줄 인자로는 받지 않습니다).")
        return 2

    spec = yaml.safe_load((Path(args.out) / "deploy-spec.yaml").read_text(encoding="utf-8"))
    app, work = spec["app"], Path(tempfile.mkdtemp(prefix="anyship-smoke-deployer-"))
    image = f"{app}:{SHA}"
    events = []

    def record(event):  # 화면에도 보여 주고, 단계 번호를 나중에 확인하려고 모아 둔다
        events.append(event)
        log(event)

    try:
        cache = Path(args.plugin_cache)
        cache.mkdir(parents=True, exist_ok=True)
        env = AwsEnvironment(
            env_id=args.env_id,
            role_arn=args.role_arn,
            external_id=external_id,
            region=args.region,
            state_bucket=args.state_bucket,
        )
        adapter = AwsAlwaysOnAdapter(
            Path(args.key), base_domain=args.domain, verify_tls=not args.staging
        )
        runner = ReadOnlyRunner(
            TerraformRunner(ROOT / "infra" / "user-account", plugin_cache_dir=cache)
        )
        settings = FoundationSettings(
            service_server_ip="203.0.113.1",
            ssh_public_key="ssh-ed25519 unused",
            acme_email="unused@example.com",
        )  # 기반을 만들지 않으므로 쓰이지 않는다
        deployer = Deployer(
            {"aws-always-on": adapter},
            ImageBuilder(timeout=900),
            runner=runner,
            foundation=settings,
        )
        url = f"https://{app}.{args.env_id}.aws.{args.domain}"
        assemble(Path(args.sample), Path(args.out), work / "source", plant=False)

        print("1) 잘못된 입력은 아무것도 시작하기 전에 거절된다")
        wrong_set = deployer.deploy(
            env, spec, work / "source", SHA, {}, lambda e: None, set_name="onprem"
        )
        expect(
            "세트와 환경이 맞지 않으면 spec 단계에서 거절",
            not wrong_set.ok and wrong_set.details["stage"] == "spec",
        )
        expect(
            "거절되었으니 이미지가 만들어지지 않았다",
            docker("image", "inspect", image).returncode != 0,
        )

        print("2) Deployer.deploy 한 번 호출")
        result = timed(
            lambda: deployer.deploy(
                env, spec, work / "source", SHA, {}, record, set_name="aws-always-on"
            )
        )
        show(result)
        expect("배포가 성공하고 주소가 맞다", result.ok and result.url == url)
        if not result.ok:
            return 1

        print("3) 로그와 결과")
        stages = [(e.step, e.total, e.name) for e in events if e.step]
        names = [name for _, _, name in dict.fromkeys(stages)]
        expect(
            "로그가 5단계로 이어진다",
            {t for _, t, _ in stages} == {5}
            and sorted({s for s, _, _ in stages}) == [1, 2, 3, 4, 5],
        )
        expect(
            "단계 이름 순서가 맞다",
            names == ["이미지 빌드", "공용 기반 확인", "연결 확인", "배포", "정리"],
        )
        expect(
            "단계 번호가 거꾸로 가지 않는다",
            [s for s, _, _ in stages] == sorted(s for s, _, _ in stages),
        )
        expect(
            "기반 값이 결과에 실린다(만들지는 않았다)",
            result.details.get("foundation_created") is False
            and all(result.details["foundation"].values()),
        )
        expect("이미지가 결과에 실린다", result.details.get("image") == image)

        print("4) 공개 주소에서 앱 확인")

        def probe(method, path, body=None):
            status, text = request(method, url + path, args.staging, body)
            print(f"      {method} {path} -> {status} {text[:100]!r}")
            return status, text

        expect("/healthz가 200이다", probe("GET", "/healthz")[0] == 200)
        status, text = probe("GET", "/todos")
        expect("DB에서 목록을 읽는다", status == 200 and isinstance(json.loads(text), list))
        status, written = probe("POST", "/todos", {"title": "파이프라인 확인용 " + SHA})
        expect("새 할 일을 쓸 수 있다(201)", status == 201)

        env_after = env.model_copy(update=result.details["foundation"])
        if args.keep:
            print(f"5) --keep: 앱을 남겼습니다: {url}")
        else:
            print("5) destroy")
            gone = timed(lambda: adapter.destroy(env_after, app, log))
            expect(
                "삭제가 성공한다",
                gone.ok and adapter.status(env_after, app).state == "not_deployed",
            )
            expect(
                "삭제 뒤 주소가 200이 아니다",
                request("GET", url + "/healthz", args.staging)[0] != 200,
            )
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            docker("image", "rm", image)

    failed = [label for label, passed in base.results if not passed]
    print(f"\n결과: {len(base.results) - len(failed)}/{len(base.results)} OK")
    for label in failed:
        print("  FAIL:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

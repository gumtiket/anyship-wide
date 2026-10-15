"""B가 변환한 샘플 앱(todo)을 ImageBuilder로 빌드하고 AwsAlwaysOnAdapter로 실제 AWS 호스트에 배포하는 수동 시험.

서비스 서버에서 실행한다. 공용 기반이 켜져 있어야 하고, `*.<환경ID>.aws.<도메인>` DNS 레코드가 있어야 한다.
이 시험은 테스트 계정의 호스트에 앱(todo)을 실제로 올렸다가 끝에 destroy한다(앱 DB `app_todo`는 남는다).

  1) 변환본(.dockerignore 포함, 심은 파일 없음)을 `todo:<SHA>`로 빌드한다
  2) TerraformRunner.read_foundation으로 공용 기반 값을 읽는다(outputs.json이 필요 없다)
  3) check -> deploy (B의 deploy-spec.yaml 그대로)
  4) 공개 주소에서 /healthz, 샘플 할 일 2개가 읽히는지(DB 연결과 시드), 새 할 일 쓰기와 읽기
  5) destroy

    EXTERNAL_ID=... python -u infra/adapters/scripts/smoke_sample_deploy.py \\
      --role-arn <역할 ARN> --state-bucket <StateBucketName> --staging

--keep을 주면 마지막에 destroy하지 않고 앱을 남긴다(브라우저로 보려면 `--staging`이 필요한 staging 인증서다).
"""

import argparse
import json
import os
import shutil
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import smoke_onprem as base
import yaml
from smoke_image_builder import assemble, docker
from smoke_onprem import expect, log, show, timed

from anyship_adapters import AwsEnvironment
from anyship_adapters.aws_always_on import AwsAlwaysOnAdapter
from anyship_adapters.image_builder import BuildError, ImageBuilder
from anyship_adapters.terraform_runner import TerraformError, TerraformRunner

ROOT = Path(__file__).resolve().parents[3]
SHA = "a1a1a1a"


def request(
    method: str, url: str, staging: bool, body: dict | None = None
) -> tuple[int | None, str]:
    context = ssl._create_unverified_context() if staging else None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=context) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, ""
    except (urllib.error.URLError, OSError):
        return None, ""


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
    app, work = spec["app"], Path(tempfile.mkdtemp(prefix="anyship-smoke-deploy-"))
    image = f"{app}:{SHA}"
    try:
        print(f"1) 변환본을 {image}로 빌드")
        assemble(Path(args.sample), Path(args.out), work / "source", plant=False)
        built = timed(
            lambda: ImageBuilder(timeout=900).build(work / "source", app, SHA, lambda e: None)
        )
        expect("이미지가 빌드된다", built.image == image)

        print("2) 공용 기반 값을 Terraform 실행기로 읽기")
        cache = Path(args.plugin_cache)
        cache.mkdir(parents=True, exist_ok=True)
        start = AwsEnvironment(
            env_id=args.env_id,
            role_arn=args.role_arn,
            external_id=external_id,
            region=args.region,
            state_bucket=args.state_bucket,
        )
        env = timed(
            lambda: TerraformRunner(
                ROOT / "infra" / "user-account", plugin_cache_dir=cache
            ).read_foundation(start, lambda e: None)
        )
        expect(
            "호스트, DB 주소, 비밀 ARN을 읽었다", all((env.host, env.db_address, env.db_secret_arn))
        )

        adapter = AwsAlwaysOnAdapter(
            Path(args.key), base_domain=args.domain, verify_tls=not args.staging
        )
        url = f"https://{app}.{args.env_id}.aws.{args.domain}"

        print("3) check -> deploy")
        checked = timed(lambda: adapter.check(env, log))
        show(checked)
        expect("check가 통과한다", checked.ok)
        deployed = timed(lambda: adapter.deploy(env, spec, SHA, {}, log, set_name="aws-always-on"))
        show(deployed)
        expect("deploy가 성공한다", deployed.ok and deployed.url == url)
        if not deployed.ok:
            return 1

        print("4) 공개 주소에서 앱 확인")

        def probe(
            method, path, body=None
        ):  # 실패했을 때 이유를 알 수 있게 상태 코드와 본문 앞부분을 보여 준다
            status, text = request(method, url + path, args.staging, body)
            print(f"      {method} {path} -> {status} {text[:100]!r}")
            return status, text

        expect("/healthz가 200이다", probe("GET", "/healthz")[0] == 200)
        status, text = probe("GET", "/todos")
        expect(
            "DB에서 목록을 읽는다(SSL 접속). 200이고 JSON 목록이다",
            status == 200 and isinstance(json.loads(text), list),
        )
        # 변환본은 시작할 때 시드를 넣지 않는다(DB 초기화를 python -m app.migrate로 분리). 앱 DB는 destroy해도 남아서 이전 실행의 항목이 있을 수 있다.
        title = "배포 확인용 할 일 " + SHA
        status, written = probe("POST", "/todos", {"title": title})
        expect("새 할 일을 쓸 수 있다(201)", status == 201)
        status, text = probe("GET", "/todos")
        expect(
            "쓴 할 일이 다시 읽힌다",
            status == 200
            and written
            and json.loads(written)["id"] in [i["id"] for i in json.loads(text)],
        )
        probe("GET", "/healthz")  # 같은 요청을 한 번 더: 처음에만 실패했는지(일시적인지) 본다
        expect("status가 running이다", adapter.status(env, app).state == "running")

        if args.keep:
            print(f"5) --keep: 앱을 남겼습니다: {url}")
        else:
            print("5) destroy")
            gone = timed(lambda: adapter.destroy(env, app, log))
            expect("삭제가 성공한다", gone.ok and adapter.status(env, app).state == "not_deployed")
            expect(
                "삭제 뒤 주소가 200이 아니다",
                request("GET", url + "/healthz", args.staging)[0] != 200,
            )
    except (BuildError, TerraformError) as exc:
        print(f"   -> 실패 {exc.error.code}: {exc.error.message}\n{exc.tail}")
        expect("시험이 중간에 멈추지 않았다", False)
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

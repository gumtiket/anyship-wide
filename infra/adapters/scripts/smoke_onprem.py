"""실제 서버에서 OnpremAdapter를 시험하는 수동 시험(자동 테스트가 아니다).

서비스 서버에서 실행한다. 작은 시험 앱 이미지를 만들고 아래를 차례로 해 본다.
check -> deploy -> 재배포 -> status -> rollback -> (없는 버전으로 rollback) -> destroy
각 단계가 기대한 결과를 내는지 직접 확인하고(OK/FAIL), 걸린 시간을 출력한다.

    python scripts/smoke_onprem.py --host 3.38.88.141 --env-id demo --staging

--keep을 주면 마지막에 destroy를 하지 않고 시험 앱을 남겨 둔다.

--dns를 주면 Route 53 레코드도 어댑터가 만들고 지운다(boto3와 서비스 서버 역할의 권한이 필요하다).
새 환경 ID로만 쓴다(레코드를 지우므로 진짜 demo 환경과는 같이 쓰지 않는다).

    python scripts/smoke_onprem.py --host 3.38.88.141 --env-id dnstest --dns --staging
"""

import argparse
import json
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from anyship_adapters import OnpremEnvironment
from anyship_adapters.onprem import OnpremAdapter

APP = "smoke"
V1, V2, MISSING = "1111111", "2222222", "3333333"

# 시험 앱: 표준 라이브러리만 쓰는 작은 웹 서버. /healthz와 /에서 200을 돌려준다.
SERVER = """import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path in ("/", "/healthz")
        body = json.dumps({
            "app": "smoke", "path": self.path,
            "has_database_url": "DATABASE_URL" in os.environ,
            "has_secret_key": "SECRET_KEY" in os.environ,
        }).encode()
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler).serve_forever()
"""
DOCKERFILE = """FROM public.ecr.aws/docker/library/python:3.12-slim
COPY server.py /app/server.py
USER 65532
CMD ["python", "/app/server.py"]
"""
SPEC = {
    "app": APP,
    "port": 8080,
    "healthcheck": "/healthz",
    "env": [
        {"name": "LOG_LEVEL", "value": "INFO"},
        {"name": "SECRET_KEY", "secret": True, "generate": True},
    ],
    "backing_services": [{"type": "postgres", "bind_as": "DATABASE_URL"}],
    "release": {"migrate": "echo migration-ran"},
}

results: list[tuple[str, bool]] = []
messages: list[str] = []  # 어댑터가 남긴 로그 문구(DNS를 바꿨는지 확인하는 데 쓴다)


def build(tag: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "server.py").write_text(SERVER, encoding="utf-8")
        Path(directory, "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
        subprocess.run(["docker", "build", "-q", "-t", f"{APP}:{tag}", directory], check=True)


def log(event) -> None:
    messages.append(event.message)
    prefix = f"[{event.step}/{event.total}] {event.name}" if event.step else f"({event.level})"
    print(f"    {prefix}: {event.message}")


def timed(call):
    started = time.perf_counter()
    result = call()
    print(f"      걸린 시간: {time.perf_counter() - started:.1f}초")
    return result


def show(result) -> None:
    print("   ->", "성공" if result.ok else f"실패 {result.error.code}: {result.error.message}")
    if not result.ok and result.error.hint:
        print("      조치:", result.error.hint)
    if result.details:
        print("      details:", json.dumps(result.details, ensure_ascii=False))


def expect(label: str, condition: bool) -> None:
    results.append((label, bool(condition)))
    print(f"      {'OK  ' if condition else 'FAIL'} {label}")


def http(url: str, staging: bool) -> tuple[int | None, str]:
    context = ssl._create_unverified_context() if staging else None
    try:
        with urllib.request.urlopen(url, timeout=10, context=context) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, ""
    except (urllib.error.URLError, OSError):
        return None, ""


def deploy(adapter, env, tag):
    build(tag)
    result = timed(lambda: adapter.deploy(env, SPEC, tag, {}, log, set_name="onprem"))
    show(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, help="온프레미스 서버 주소")
    parser.add_argument("--env-id", default="demo")
    parser.add_argument("--key", default=str(Path.home() / ".ssh" / "onprem_deploy"))
    parser.add_argument("--domain", default="anyship.cloud")
    parser.add_argument(
        "--staging", action="store_true", help="Let's Encrypt staging 인증서면 TLS 검증을 끈다"
    )
    parser.add_argument(
        "--keep", action="store_true", help="마지막에 destroy하지 않고 시험 앱을 남긴다"
    )
    parser.add_argument(
        "--dns", action="store_true", help="Route 53 레코드도 어댑터가 만들고 지운다"
    )
    args = parser.parse_args()
    if args.dns and args.env_id == "demo":
        print(
            "--dns는 진짜 demo 환경과 같이 쓸 수 없습니다(끝에서 환경의 레코드를 지웁니다). 다른 --env-id를 쓰세요."
        )
        return 2

    env = OnpremEnvironment(env_id=args.env_id, host=args.host)
    records = None
    if args.dns:
        from anyship_adapters.dns import WildcardRecords

        records = WildcardRecords(base_domain=args.domain)
        if records.remove(args.env_id):  # 항상 레코드가 없는 새 환경에서 시작한다
            print(f"(이전 시험이 남긴 {args.env_id} 레코드를 지웠습니다)")
    adapter = OnpremAdapter(
        Path(args.key), base_domain=args.domain, verify_tls=not args.staging, dns=records
    )
    url = f"https://{APP}.{args.env_id}.onprem.{args.domain}"

    print("1) check")
    checked = timed(lambda: adapter.check(env, log))
    show(checked)
    if not checked.ok:
        return 1

    print(f"2) deploy {APP}:{V1}")
    first = deploy(adapter, env, V1)
    expect("배포가 성공한다", first.ok)
    if not first.ok:
        return 1
    expect("공개 주소가 200으로 응답한다", http(url + "/", args.staging)[0] == 200)
    if args.dns:
        expect("첫 배포가 DNS 레코드를 새로 만들었다", any("새로 반영" in m for m in messages))
        messages.clear()

    print(f"3) 재배포 {APP}:{V2} (서버의 비밀을 재사용하는지)")
    second = deploy(adapter, env, V2)
    expect("재배포가 성공한다", second.ok)
    if args.dns:
        expect("재배포는 DNS 레코드를 건드리지 않았다", any("이미 맞습니다" in m for m in messages))
    expect(
        "새로 만든 비밀이 없다(서버의 비밀을 재사용)",
        second.ok and second.details.get("generated") == [],
    )
    expect("재배포 뒤에도 200으로 응답한다", http(url + "/", args.staging)[0] == 200)

    print("4) status")
    state = timed(lambda: adapter.status(env, APP))
    expect(
        "실행 중이고 최신 버전이다", state.ok and state.state == "running" and state.image_tag == V2
    )

    print(f"5) rollback {V1}")
    back = timed(lambda: adapter.rollback(env, APP, V1, log))
    show(back)
    expect("롤백이 성공한다", back.ok and back.image_tag == V1)
    expect("롤백 뒤에도 200으로 응답한다", http(url + "/", args.staging)[0] == 200)
    expect("상태가 이전 버전을 가리킨다", adapter.status(env, APP).image_tag == V1)

    print(f"6) 서버에 없는 버전 {MISSING}으로 rollback (거부되어야 한다)")
    missing = adapter.rollback(env, APP, MISSING, log)
    show(missing)
    expect("image_not_found로 거부된다", not missing.ok and missing.error.code == "image_not_found")
    expect("거부된 뒤에도 이전 버전이 그대로 실행 중이다", adapter.status(env, APP).image_tag == V1)

    if args.keep:
        print("7) --keep: destroy를 하지 않고 시험 앱을 남겼습니다.")
    else:
        print("7) destroy")
        gone = timed(lambda: adapter.destroy(env, APP, log))
        show(gone)
        expect("삭제가 성공한다", gone.ok)
        expect("삭제 뒤 상태가 not_deployed이다", adapter.status(env, APP).state == "not_deployed")
        expect("삭제 뒤 주소가 더 이상 200이 아니다", http(url + "/", args.staging)[0] != 200)
        expect("다시 삭제해도 성공한다(여러 번 해도 안전)", adapter.destroy(env, APP, log).ok)
        if args.dns:
            print("8) 환경의 DNS 레코드 삭제")
            messages.clear()
            dns_gone = timed(lambda: adapter.remove_environment_dns(env, log))
            show(dns_gone)
            expect(
                "환경 DNS 삭제가 성공한다", dns_gone.ok and any("지웠습니다" in m for m in messages)
            )
            expect(
                "레코드가 실제로 없어졌다(다시 지울 게 없다)", records.remove(args.env_id) is False
            )

    failed = [label for label, ok in results if not ok]
    print(f"\n결과: {len(results) - len(failed)}/{len(results)} OK")
    for label in failed:
        print("  FAIL:", label)
    print(
        "시험에 쓴 이미지 정리(서버): docker image rm "
        f"{APP}:{V1} {APP}:{V2}   / (서비스 서버): docker image rm {APP}:{V1} {APP}:{V2}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

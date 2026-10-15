"""실제 배포 API를 서비스 서버에서 처음부터 끝까지 시험하는 수동 시험(자동 테스트가 아니다). 진짜 앱, 진짜 실행기, 진짜 AWS를 쓴다.

`seed_test_environment.py`로 만든 시험용 SQLite DB를 쓰므로 서비스 운영 DB는 건드리지 않는다. 프로세스 안에서 앱을 띄워 API를 부르고
(포트를 열지 않는다), 로그인은 시험용 세션을 DB에 직접 넣어 대신한다(쿠키와 토큰을 화면에 출력하지 않는다). 확인하는 것:
  1) 앱이 real 모드로 뜨고, 환경이 선택 가능하다
  2) 대상을 저장하고 배포를 요청하면 진행 로그가 단계별로 흐르고 작업이 성공한다
  3) 공개 주소가 200이고, 이력, 현재 버전, 환경에 저장된 기반 값이 맞다
  4) 같은 요청 ID로 다시 보내도 새 작업이 생기지 않는다
  5) 로그, 결과, DB 어디에도 External ID가 없다
  6) 끝나면 앱을 지운다(--keep을 주면 남긴다)

    cd service
    APP_DEPLOY_SOURCE_DIR=~/anyship-sources APP_DEPLOY_SSH_KEY=~/.ssh/onprem_deploy \\
    APP_DEPLOY_SERVICE_IP=<이 서버의 공개 IP> APP_DEPLOY_ACME_EMAIL=<이메일> APP_DEPLOY_BASE_DOMAIN=anyship.cloud \\
    APP_DEPLOY_VERIFY_TLS=false \\
    python -u ../scripts/smoke_deploy_api.py --database ~/anyship-test.db --environment-id <시드가 출력한 ID>

소스는 `<APP_DEPLOY_SOURCE_DIR>/<저장소 이름>/`에 있어야 한다(`make_sample_repo.py`로 만든다). 이 스크립트는 가짜 로그인 세션을 쓰므로
GitHub 토큰이 없어서 **소스를 항상 로컬 폴더로 읽는다**(`APP_DEPLOY_SOURCE=local`로 고정). GitHub에서 받는 경로는 웹 화면으로 확인한다.
첫 배포에 공용 기반이 없으면 약 20분이 걸린다.
"""

import argparse
import hashlib
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.app import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import AwsEnvironment, DeployJob, Deployment, LoginSession, Project, database  # noqa: E402
from app.deploy_state import adapter_environment  # noqa: E402

ORIGIN = "http://localhost:8000"
results: list[tuple[str, bool]] = []


def expect(label: str, passed: bool) -> None:
    results.append((label, bool(passed)))
    print(f"   {'OK  ' if passed else 'FAIL'} {label}")


def status_of(url: str, verify: bool) -> int | None:
    context = None if verify else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(url, timeout=15, context=context) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return None


def summary() -> int:
    failed = [label for label, passed in results if not passed]
    print(f"\n결과: {len(results) - len(failed)}/{len(results)} OK")
    for label in failed:
        print("  FAIL:", label)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database", required=True, help="seed_test_environment.py로 만든 SQLite 파일"
    )
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--repo-name", default="todo", help="소스 폴더 이름이 되는 저장소 이름")
    parser.add_argument("--keep", action="store_true", help="끝나도 앱을 지우지 않는다")
    parser.add_argument(
        "--create-foundation",
        action="store_true",
        help="공용 기반이 없으면 만들도록 허용한다(약 20분, 요금 발생). 기본은 기반을 만들지 않고 멈춘다",
    )
    parser.add_argument("--timeout-minutes", type=int, default=40)
    args = parser.parse_args()

    path = Path(args.database).expanduser().resolve()
    if not path.is_file():
        print("시험용 DB 파일이 없습니다. 먼저 seed_test_environment.py를 실행하세요.")
        return 2
    url = f"sqlite:///{path.as_posix()}"
    # 운영 DB를 가리키지 않도록 이 스크립트가 직접 만든 설정만 쓴다. 배포 설정(APP_DEPLOY_*)만 환경변수에서 읽는다.
    settings = Settings(
        database_url=url,
        app_origin=ORIGIN,
        token_key=Fernet.generate_key().decode(),
        github_client_id="local",
        github_client_secret="local",
        github_app_slug="local",
        deployment_mode="real",
        **{**Settings._deploy_from_env(), "deploy_source": "local"},
    )  # 가짜 로그인에는 GitHub 토큰이 없어 폴더 소스로 고정한다
    engine, sessions = database(url)

    with sessions() as session:
        environment = session.get(AwsEnvironment, args.environment_id)
        if environment is None or environment.status != "CONNECTED":
            print("시험용 DB에 CONNECTED 환경이 없습니다.")
            return 2
        external_id, user_id, workspace_id = (
            environment.external_id,
            environment.created_by,
            environment.workspace_id,
        )
        project = session.scalar(select(Project).where(Project.workspace_id == workspace_id))
        if project is None:
            project = Project(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                repository_id=1,
                installation_id=1,
                full_name=f"owner/{args.repo_name}",
                branch="main",
                base_sha="a" * 40,
                created_by=user_id,
                created_at=int(time.time()),
            )
            session.add(project)
        raw = uuid.uuid4().hex + uuid.uuid4().hex
        csrf = uuid.uuid4().hex
        session.add(
            LoginSession(
                id=hashlib.sha256(raw.encode()).hexdigest(),
                user_id=user_id,
                workspace_id=workspace_id,
                token_cipher="unused",
                csrf=csrf,
                expires_at=int(time.time()) + 3 * 3600,
            )
        )
        session.commit()
        project_id = project.id

    app = create_app(settings)
    if not args.create_foundation:
        # env_id가 켜 둔 기반과 다르면 Deployer가 20분짜리 apply를 시작한다. 실수로 새 기반이 만들어지지 않게 apply를 막는다.
        deployer = app.state.deploy_runner.deployer
        real_runner = deployer._runner

        class ReadOnlyRunner:
            def read_foundation(self, env, log):
                return real_runner.read_foundation(env, log)

            def apply(self, env, variables, log):
                raise RuntimeError(
                    "기반이 없어서 만들어야 하지만 --create-foundation 없이는 만들지 않습니다."
                )

        deployer._runner = ReadOnlyRunner()
    endpoint = f"/api/projects/{project_id}/deployment"
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    started = time.monotonic()
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            client.cookies.set("app_session", raw)

            print("1) real 모드와 환경")
            expect(
                "deployment_mode가 real이다",
                client.get("/api/config").json()["deployment_mode"] == "real",
            )
            overview = client.get(endpoint)
            expect("개요를 읽는다", overview.status_code == 200)
            if overview.status_code != 200:
                print(f"      -> {overview.status_code} {overview.text[:200]}")
                return 1
            expect(
                "환경이 선택 가능하다",
                any(
                    e["id"] == args.environment_id and e["available"]
                    for e in overview.json()["environments"]
                ),
            )

            print("2) 대상 저장과 배포 요청")
            saved = client.put(
                endpoint,
                headers=headers,
                json={"environment_id": args.environment_id, "set_name": "aws-always-on"},
            )
            expect("대상을 저장한다", saved.status_code == 200)
            request_id = str(uuid.uuid4())
            accepted = client.post(
                endpoint + "/jobs", headers=headers, json={"request_id": request_id}
            )
            print(
                f"      -> {accepted.status_code} {accepted.json().get('status') if accepted.status_code < 300 else accepted.text[:200]}"
            )
            expect("배포 요청이 접수된다(202)", accepted.status_code == 202)
            if accepted.status_code != 202:
                return 1
            job_id, seen, job = accepted.json()["id"], 0, accepted.json()
            deadline = time.monotonic() + args.timeout_minutes * 60
            while job["status"] in ("queued", "running") and time.monotonic() < deadline:
                time.sleep(2)
                job = client.get(f"{endpoint}/jobs/{job_id}").json()
                for entry in job["logs"][seen:]:
                    print(
                        f"    [{entry.get('step', 0)}/{entry.get('total', 0)}] {entry.get('name') or ''} {entry['message'][:140]}"
                    )
                seen = len(job["logs"])
            print(
                f"      -> 상태 {job['status']}, 단계 {job['stage'] or '-'}, 경과 {int(time.monotonic() - started)}초"
            )
            if job["status"] != "succeeded":
                print(
                    "      오류:", json.dumps(job["result"].get("error"), ensure_ascii=False)[:400]
                )
            expect("작업이 성공한다", job["status"] == "succeeded")
            if job["status"] != "succeeded":
                return summary()  # 실패한 뒤에는 주소도 버전도 없으니 다음 단계를 건너뛴다

            print("3) 결과")
            target = client.get(endpoint).json()["target"]
            app_url = target["url"]
            print(f"      주소 {app_url}")
            expect(
                "현재 버전과 주소가 기록된다",
                target["deployed"] and bool(target["image_tag"]) and app_url.startswith("https://"),
            )
            expect("작업의 이미지 태그가 현재 버전과 같다", job["image_tag"] == target["image_tag"])
            expect(
                "공개 주소의 /healthz가 200이다",
                status_of(app_url + "/healthz", settings.deploy_verify_tls) == 200,
            )
            with sessions() as session:
                saved_env = session.get(AwsEnvironment, args.environment_id)
                expect(
                    "환경에 기반 값과 state_bucket이 저장된다",
                    all(
                        (
                            saved_env.host,
                            saved_env.db_address,
                            saved_env.db_secret_arn,
                            saved_env.state_bucket,
                        )
                    ),
                )

            print("4) 같은 요청 ID")
            again = client.post(
                endpoint + "/jobs", headers=headers, json={"request_id": request_id}
            )
            expect(
                "같은 작업을 돌려준다(200)",
                again.status_code == 200 and again.json()["id"] == job_id,
            )
            with sessions() as session:
                expect("작업이 하나뿐이다", len(list(session.scalars(select(DeployJob.id)))) == 1)

            print("5) 비밀")
            with sessions() as session:
                dump = json.dumps(
                    [
                        [str(getattr(row, c.name)) for c in row.__table__.columns]
                        for model in (DeployJob, Deployment)
                        for row in session.scalars(select(model))
                    ]
                )
            expect(
                "작업 기록과 응답에 External ID가 없다",
                external_id not in dump and external_id not in json.dumps(job),
            )

            if args.keep:
                print(f"6) --keep: 앱을 남겼습니다: {app_url}")
            else:
                print("6) 앱 삭제")
                adapter = app.state.deploy_runner.deployer._adapters["aws-always-on"]
                with sessions() as session:
                    gone = adapter.destroy(
                        adapter_environment(session.get(AwsEnvironment, args.environment_id)),
                        target["app_name"],
                        lambda event: None,
                    )
                expect("삭제가 성공한다", gone.ok)
    finally:
        engine.dispose()
    return summary()


if __name__ == "__main__":
    sys.exit(main())

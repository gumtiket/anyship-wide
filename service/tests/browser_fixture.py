"""Manual browser QA harness. Only fake GitHub HTTP; separate temporary DB/port.

Run from service: python -m tests.browser_fixture
Open http://127.0.0.1:8001/_test_only/login. Never used by app.main or launch scripts.
--deploy: real deployment screen with a fake source/deployer (no AWS, no Docker); jobs take a few seconds.
         /_test_only/fail-next-destroy makes the next removal fail once (to see the error screen).
--ai: real AI subprocess with a fake model and GitHub; never makes paid calls or remote writes.
--onprem: registration and deployment with a fake connect factory (no SSH, Docker or DNS calls).
          /_test_only/onprem/ready simulates the latest registered server's readiness report.
"""

import json
import sys
import time
import uuid
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import uvicorn
from cryptography.fernet import Fernet
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.testclient import TestClient

from app.app import create_app
from app.config import Settings
from app.db import Base

from .test_real_workflow import GitHubHTTP, login


def deploy_runner(directory, database_url):
    """화면 확인용 실행기: 진짜 스레드와 진짜 작업 처리, 가짜 소스와 배포자. 몇 초에 걸쳐 단계 로그를 흘린다."""
    from pathlib import Path as P
    from types import SimpleNamespace

    from anyship_adapters import AdapterError, DeployResult, DestroyResult, LogEvent
    from app.db import database as open_database
    from app.deploy_runner import DeployRunner

    from .test_deploy_runner import FOUNDATION, SHA, FakeAccess, FakeDeployer, FakeSource

    def slow(log, secrets):
        names = ["이미지 빌드", "공용 기반 확인", "연결 확인", "배포", "정리"]
        for number, name in enumerate(names, start=1):
            time.sleep(1.2)
            log(LogEvent(message=f"{name} 진행 중", step=number, total=5, name=name))
        if "FAIL" in secrets:
            return DeployResult(
                ok=False,
                error=AdapterError(
                    code="healthcheck_failed",
                    message="앱이 /healthz에 응답하지 않았습니다.",
                    hint="앱 로그를 확인해 주세요.",
                    retryable=True,
                ),
                details={"stage": "deploy"},
            )
        return DeployResult(
            ok=True,
            url="https://todo.test.aws.anyship.cloud",
            image_tag=SHA,
            details={"foundation": FOUNDATION, "foundation_created": False},
        )

    state = {
        "fail_next_destroy": False
    }  # /_test_only/fail-next-destroy로 다음 삭제 한 번을 실패시킨다

    def slow_destroy(log):
        for number, name in enumerate(["컨테이너와 볼륨 제거", "서버의 앱 폴더 제거"], start=1):
            time.sleep(1.2)
            log(LogEvent(message=f"{name} 진행 중", step=number, total=2, name="앱 삭제"))
        if state["fail_next_destroy"]:
            state["fail_next_destroy"] = False
            return DestroyResult(
                ok=False,
                error=AdapterError(
                    code="destroy_failed",
                    message="컨테이너를 지우지 못했습니다.",
                    hint="서버에서 docker compose down을 확인해 주세요.",
                ),
            )
        return DestroyResult(ok=True)

    _, sessions = open_database(database_url)
    settings = SimpleNamespace(database_url=database_url)
    runner = DeployRunner(
        settings,
        sessions,
        source=FakeSource(),
        deployer=FakeDeployer(slow, destroy_behaviour=slow_destroy),
        access=FakeAccess(),
        lock_dir=P(directory) / "deploy-lock",
    )
    runner.test_state = state
    return runner


def seed_environment(app, client):
    """로그인한 사용자에게 연결된 AWS 환경 한 건을 만든다(화면 확인용)."""
    from sqlalchemy import select

    from app.db import AwsEnvironment, User

    from .test_deploy_runner import BUCKET, ROLE

    workspace = client.get("/api/me").json()["workspace"]["id"]
    with app.state.sessions() as session:
        user = session.scalar(select(User.id))
        identifier = str(uuid.uuid4())
        session.add(
            AwsEnvironment(
                id=identifier,
                workspace_id=workspace,
                created_by=user,
                request_id=identifier,
                name="테스트 계정",
                region="ap-northeast-2",
                external_id=identifier.replace("-", "") * 2,
                template_url="https://example.s3.amazonaws.com/a.yaml",
                service_role_arn="arn:aws:iam::999999999999:role/s",
                stack_name="anyship-onboarding-x",
                role_name="deploy-service-role",
                status="CONNECTED",
                role_arn=ROLE,
                aws_account_id="223455088214",
                created_at=1,
                expires_at=2,
                env_id="test",
                state_bucket=BUCKET,
            )
        )
        session.commit()


def main():
    with TemporaryDirectory(prefix="anyship-browser-test-") as directory:
        ai = "--ai" in sys.argv
        if ai:
            from .test_ai_integration import AnalysisGitHub

            sample = Path(__file__).resolve().parents[2] / "AI/samples/todo"
            fixture = AnalysisGitHub(
                {
                    p.relative_to(sample).as_posix(): p.read_bytes().decode().replace("\r\n", "\n")
                    for p in sample.rglob("*")
                    if p.is_file()
                }
            )
        else:
            fixture = GitHubHTTP()
        onboarding = "--onboarding" in sys.argv
        mock = "--mock" in sys.argv
        onprem = "--onprem" in sys.argv
        deploy = "--deploy" in sys.argv or onprem
        fixture.repo_selected = not onboarding
        with httpx.Client(transport=httpx.MockTransport(fixture)) as remote:
            # Every outgoing request is intercepted; unknown routes fail closed.
            httpx.request = remote.request
            database_url = f"sqlite:///{Path(directory) / 'test.db'}"
            runner = deploy_runner(directory, database_url) if deploy else None
            if onprem:
                from app.onprem_runner import OnpremRunner

                from .onprem_fakes import RecordingTransport, load_fixture, parts

                transport = RecordingTransport(Path(directory))
                transport.settings.app_origin = "http://127.0.0.1:8001"
                runner.onprem = OnpremRunner(runner, parts().deployer, transport)
                original = runner.source.fetch

                def onprem_source(*args):
                    fetched = original(*args)
                    return fetched.__class__(
                        path=fetched.path,
                        commit_sha=fetched.commit_sha,
                        spec=load_fixture("specs").GENERATED,
                        cleanup=fetched.cleanup,
                    )

                runner.source.fetch = onprem_source
            real = (
                dict(
                    deploy_source_dir=Path(directory),
                    deploy_ssh_key=Path(directory) / "key",
                    deploy_service_ip="203.0.113.10",
                    deploy_acme_email="ops@example.com",
                    deploy_base_domain="anyship.cloud",
                )
                if deploy
                else {}
            )
            app = create_app(
                Settings(
                    app_origin="http://127.0.0.1:8001",
                    database_url=database_url,
                    token_key=Fernet.generate_key().decode(),
                    github_client_id="fixture",
                    github_client_secret="fixture",
                    github_app_slug="fixture",
                    ai_mode="bronze" if ai else "placeholder",
                    ai_provider="fake",
                    ai_workspace=Path(directory) / "ai",
                    deployment_mode="mock" if mock else "real" if deploy else "unavailable",
                    mock_step_delay=0.15,
                    **real,
                ),
                deploy_runner=runner,
            )
            Base.metadata.create_all(app.state.engine)
            if onprem:

                @app.get("/_test_only/onprem/ready")
                def onprem_ready():
                    identifier = next(reversed(transport.tokens))
                    local = TestClient(app, base_url="http://127.0.0.1:8001")
                    try:
                        result = local.post(
                            f"/api/onprem/environments/{identifier}/ready",
                            json={
                                "token": transport.tokens[identifier],
                                "public_ip": "3.38.88.141",
                            },
                        )
                        if result.status_code != 202:
                            return JSONResponse(
                                {"error": "test report rejected"}, status_code=result.status_code
                            )
                    finally:
                        local.close()
                    return RedirectResponse("/#onprem")

            if deploy:

                @app.get("/_test_only/fail-next-destroy")
                def fail_next_destroy():
                    runner.test_state["fail_next_destroy"] = True
                    return {"ok": True}

            @app.get("/_test_only/login")
            def test_login():
                response = RedirectResponse("/")
                response.set_cookie("app_session", cookie, httponly=True, samesite="lax")
                return response

            if onboarding:
                # Only this explicitly launched harness replaces the external approval page.
                @app.middleware("http")
                async def local_approval(request, call_next):
                    response = await call_next(request)
                    if (
                        request.url.path == "/api/github/connection/authorize"
                        and response.status_code == 200
                    ):
                        body = b"".join([part async for part in response.body_iterator])
                        state = parse_qs(urlsplit(json.loads(body)["url"]).query)["state"][0]
                        return JSONResponse(
                            {"url": "/_test_only/permissions?" + urlencode({"state": state})}
                        )
                    return response

                @app.get("/_test_only/permissions")
                def permissions(state: str):
                    target = escape(
                        "/_test_only/complete?" + urlencode({"state": state}), quote=True
                    )
                    return HTMLResponse(
                        f'<html lang="ko"><title>테스트 전용 승인 화면</title><h1>테스트 전용 승인 화면</h1><p>실제 GitHub 권한은 변경하지 않습니다.</p><a href="{target}">테스트 접근 허용</a></html>'
                    )

                @app.get("/_test_only/complete")
                def complete(state: str):
                    fixture.repo_selected = True
                    return RedirectResponse(
                        "/?"
                        + urlencode(
                            {"state": state, "installation_id": "9", "setup_action": "install"}
                        )
                    )

            with TestClient(app, base_url="http://127.0.0.1:8001") as client:
                login(client)
                cookie = client.cookies.get("app_session")
                if deploy:
                    seed_environment(app, client)
                if mock or deploy or ai:
                    project = client.post(
                        "/api/projects",
                        json={
                            "repository_url": "https://github.com/owner/real-repo",
                            "branch": "main",
                        },
                        headers={
                            "Origin": "http://127.0.0.1:8001",
                            "X-CSRF-Token": client.get("/api/me").json()["csrf_token"],
                        },
                    )
                    assert project.status_code == 201, project.text
            uvicorn.run(app, host="127.0.0.1", port=8001, access_log=False)


if __name__ == "__main__":
    main()

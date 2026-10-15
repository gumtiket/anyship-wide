"""서비스 → 인프라 MockAdapter 통합 테스트. 외부 클라우드나 저장소에 쓰지 않는다."""

import json
import threading
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from anyship_adapters import LogEvent, MockAdapter
from app.app import create_app
from app.config import Settings
from app.db import Base, MockDeployment, MockJob
from tests.test_aws_onboarding import SERVICE_ROLE, TEMPLATE, authenticate, create, save_role
from tests.test_web import ORIGIN, FakeGitHub, add


@pytest.fixture
def mock_web(database_url):
    settings = Settings(
        database_url=database_url,
        token_key=Fernet.generate_key().decode(),
        github_client_id="fixture",
        github_client_secret="fixture",
        github_app_slug="fixture",
        aws_template_url=TEMPLATE,
        aws_service_role_arn=SERVICE_ROLE,
        aws_regions=("ap-northeast-2",),
        deployment_mode="mock",
        mock_step_delay=0,
    )
    app = create_app(settings, FakeGitHub())
    Base.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=ORIGIN) as client:
        headers = authenticate(client)
        project = add(client, headers).json()
        yield SimpleNamespace(
            app=app,
            client=client,
            headers=headers,
            settings=settings,
            project=project,
            endpoint=f"/api/projects/{project['id']}/mock-deployment",
        )


def select_target(web, source="sample-aws", set_name="aws-always-on"):
    response = web.client.put(
        web.endpoint, headers=web.headers, json={"source": source, "set_name": set_name}
    )
    assert response.status_code == 200, response.text
    return response.json()


def submit(web, action="check", tag="", scenario="success", request_id=None):
    return web.client.post(
        web.endpoint + "/jobs",
        headers=web.headers,
        json={
            "request_id": request_id or str(uuid.uuid4()),
            "action": action,
            "image_tag": tag,
            "scenario": scenario,
        },
    )


def finish(web, response):
    assert response.status_code in (200, 202), response.text
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        row = web.client.get(web.endpoint + "/jobs/" + response.json()["id"]).json()
        if row["status"] not in ("queued", "running"):
            return row
        time.sleep(0.01)
    pytest.fail("Mock job did not finish")


def run(web, action="check", tag="", scenario="success"):
    return finish(web, submit(web, action, tag, scenario))


def current(web):
    response = web.client.get(web.endpoint)
    assert response.status_code == 200, response.text
    return response.json()["target"]


@pytest.mark.parametrize(
    "source,set_name",
    [
        ("sample-aws", "aws-always-on"),
        ("sample-aws", "aws-serverless"),
        ("sample-onprem", "onprem"),
    ],
)
def test_full_lifecycle_and_version_history(mock_web, source, set_name):
    web = mock_web
    target = select_target(web, source, set_name)
    assert target["mock"] and not target["checked"] and target["state"] == "not_deployed"
    assert submit(web, "deploy", "aaaaaaa").status_code == 409
    checked = run(web)
    assert checked["status"] == "succeeded" and len(checked["logs"]) == 2
    assert current(web)["checked"]
    deployed = run(web, "deploy", "aaaaaaa")
    assert deployed["status"] == "succeeded"
    assert [log["step"] for log in deployed["logs"]] == [1, 2, 1, 2, 3, 4, 5]
    assert current(web)["image_tag"] == "aaaaaaa"
    assert run(web, "deploy", "bbbbbbb")["status"] == "succeeded"
    assert set(current(web)["versions"]) == {"aaaaaaa", "bbbbbbb"}
    assert submit(web, "rollback", "ccccccc").status_code == 409
    assert run(web, "rollback", "aaaaaaa")["status"] == "succeeded"
    assert current(web)["image_tag"] == "aaaaaaa"
    assert (
        web.client.put(
            web.endpoint,
            headers=web.headers,
            json={
                "source": "sample-onprem" if source == "sample-aws" else "sample-aws",
                "set_name": "onprem" if source == "sample-aws" else "aws-always-on",
            },
        ).status_code
        == 409
    )
    removed = run(web, "destroy")
    assert removed["status"] == "succeeded"
    assert current(web)["state"] == "not_deployed" and current(web)["example_url"] is None
    assert len(web.client.get(web.endpoint + "/jobs").json()) == 5
    assert web.client.get(web.endpoint + "/jobs").json()[0]["id"] == removed["id"]
    assert len(web.client.get(web.endpoint + "/jobs?limit=2&offset=2").json()) == 2
    assert web.client.get(web.endpoint + "/jobs?limit=0").status_code == 422
    # 등록 삭제는 로컬 DB의 모의 배포 기록과 선택만 지운다.
    assert (
        web.client.delete(f"/api/projects/{web.project['id']}", headers=web.headers).status_code
        == 204
    )
    with web.app.state.sessions() as session:
        assert session.get(MockDeployment, web.project["id"]) is None
        assert not session.scalar(select(MockJob.id))


@pytest.mark.parametrize(
    "scenario,action,code",
    [
        ("check_fails", "check", "assume_role_denied"),
        ("check_fails", "deploy", "assume_role_denied"),
        ("deploy_fails", "deploy", "container_start_failed"),
        ("unhealthy", "deploy", "healthcheck_failed"),
    ],
)
def test_failure_scenarios_retain_cause_and_allow_new_request(mock_web, scenario, action, code):
    select_target(mock_web)
    if action == "deploy":
        run(mock_web)
    failed = run(mock_web, action, "aaaaaaa" if action == "deploy" else "", scenario)
    assert failed["status"] == "failed" and failed["result"]["error"]["code"] == code
    assert failed["result"]["error"]["retryable"]
    assert current(mock_web)["state"] == "not_deployed"
    if scenario == "check_fails":
        assert not current(mock_web)["checked"]
    assert run(mock_web)["status"] == "succeeded"


def test_real_aws_registration_is_never_marked_verified(mock_web):
    web = mock_web
    environment = create(web.client, web.headers).json()
    assert save_role(web.client, web.headers, environment["id"]).status_code == 200
    original = web.client.get(f"/api/aws/environments/{environment['id']}").json()
    select_target(web, environment["id"])
    with web.app.state.sessions() as session:
        identifier = session.get(MockDeployment, web.project["id"]).adapter_env_id
        assert len(identifier) == 21 and identifier[0].islower()
    assert run(web)["result"]["details"]["account_id"] == "123456789012"
    assert run(web, "deploy", "aaaaaaa")["status"] == "succeeded"
    assert select_target(web, environment["id"])["checked"]
    with web.app.state.sessions() as session:
        assert session.get(MockDeployment, web.project["id"]).adapter_env_id == identifier
    assert web.client.get(f"/api/aws/environments/{environment['id']}").json() == original
    config = web.client.get("/api/config").json()
    assert config["deployment_mode"] == "mock" and not config["aws_verification_available"]
    assert (
        web.client.delete(
            f"/api/aws/environments/{environment['id']}", headers=web.headers
        ).status_code
        == 204
    )
    assert current(web) is None
    assert len(web.client.get(web.endpoint + "/jobs").json()) == 2


def test_async_idempotency_conflicts_and_deletion_guards(mock_web):
    web = mock_web
    environment = create(web.client, web.headers).json()
    save_role(web.client, web.headers, environment["id"])
    select_target(web, environment["id"])
    entered, release = threading.Event(), threading.Event()

    class Blocking(MockAdapter):
        def check(self, env, log):
            entered.set()
            assert release.wait(5)
            return super().check(env, log)

    web.app.state.mock_runner.adapters.clear()
    web.app.state.mock_runner.adapter_factory = Blocking
    request_id = str(uuid.uuid4())
    first = submit(web, request_id=request_id)
    try:
        assert entered.wait(3) and first.status_code == 202
        again = submit(web, request_id=request_id)
        assert again.status_code == 200 and first.json()["id"] == again.json()["id"]
        assert submit(web, scenario="check_fails", request_id=request_id).status_code == 409
        assert submit(web).status_code == 409
        assert (
            web.client.delete(f"/api/projects/{web.project['id']}", headers=web.headers).status_code
            == 409
        )
        assert (
            web.client.delete(
                f"/api/aws/environments/{environment['id']}", headers=web.headers
            ).status_code
            == 409
        )
        assert (
            web.client.put(
                web.endpoint,
                headers=web.headers,
                json={"source": "sample-onprem", "set_name": "onprem"},
            ).status_code
            == 409
        )
    finally:
        release.set()
    assert finish(web, first)["status"] == "succeeded"
    assert len(web.client.get(web.endpoint + "/jobs").json()) == 1


def test_owner_csrf_and_input_validation(mock_web):
    web = mock_web
    body = {"source": "sample-aws", "set_name": "aws-always-on"}
    assert web.client.put(web.endpoint, json=body).status_code == 403
    assert (
        web.client.put(
            web.endpoint, json=body, headers={**web.headers, "Origin": "https://bad.invalid"}
        ).status_code
        == 403
    )
    assert (
        web.client.put(
            web.endpoint, json=body, headers={**web.headers, "X-CSRF-Token": "bad"}
        ).status_code
        == 403
    )
    assert (
        web.client.put(
            web.endpoint, json={**body, "source": str(uuid.uuid4())}, headers=web.headers
        ).status_code
        == 404
    )
    assert (
        web.client.put(
            web.endpoint, json={**body, "set_name": "onprem"}, headers=web.headers
        ).status_code
        == 422
    )
    select_target(web)
    job = run(web)
    assert submit(web, "deploy", "latest").status_code == 422
    assert submit(web, "deploy", "").status_code == 422
    assert submit(web, "check", "aaaaaaa").status_code == 422
    body = {
        "request_id": str(uuid.uuid4()),
        "action": "deploy",
        "image_tag": "aaaaaaa",
        "secrets": {"TOKEN": "not-accepted"},
    }
    rejected = web.client.post(web.endpoint + "/jobs", headers=web.headers, json=body)
    assert rejected.status_code == 422 and "not-accepted" not in rejected.text
    with TestClient(
        create_app(replace(web.settings, deployment_mode="unavailable"), FakeGitHub()),
        base_url=ORIGIN,
    ) as outsider:
        assert outsider.get(web.endpoint).status_code == 401
        # 같은 워크스페이스 구성원이라도 다른 사용자는 소유자의 모의 작업을 볼 수 없다.
        from app.db import LoginSession, Membership

        gateway = FakeGitHub()
        gateway.profile = lambda token: {"id": 9999, "login": "other"}
        other_app = create_app(replace(web.settings, deployment_mode="unavailable"), gateway)
        with TestClient(other_app, base_url=ORIGIN) as other:
            headers = authenticate(other)
            with web.app.state.sessions() as session:
                row = session.scalar(
                    select(LoginSession).where(LoginSession.csrf == headers["X-CSRF-Token"])
                )
                me = web.client.get("/api/me").json()
                session.add(
                    Membership(
                        user_id=row.user_id, workspace_id=me["workspace"]["id"], role="owner"
                    )
                )
                row.workspace_id = me["workspace"]["id"]
                session.commit()
            # 작업자를 새로 띄우지 않고, 실행 중인 모의 앱에 그 쿠키를 다시 쓴다.
            visitor = TestClient(web.app, base_url=ORIGIN)
            visitor.cookies.update(other.cookies)
            assert visitor.get(web.endpoint).status_code == 404
            assert visitor.get(web.endpoint + "/jobs/" + job["id"]).status_code == 404
            assert (
                visitor.post(
                    web.endpoint + "/jobs",
                    headers=headers,
                    json={"request_id": str(uuid.uuid4()), "action": "check"},
                ).status_code
                == 404
            )


def test_restart_keeps_history_but_clears_memory_and_interrupts_abandoned_jobs(mock_web):
    web = mock_web
    select_target(web)
    run(web)
    deployed = run(web, "deploy", "aaaaaaa")
    runner = web.app.state.mock_runner
    runner.close()
    abandoned_id = str(uuid.uuid4())
    with web.app.state.sessions() as session:
        target = session.get(MockDeployment, web.project["id"])
        stable_id = target.adapter_env_id
        target.active_job_id = abandoned_id
        session.add(
            MockJob(
                id=abandoned_id,
                project_id=web.project["id"],
                request_id=str(uuid.uuid4()),
                runtime_id=runner.runtime_id,
                adapter_env_id=stable_id,
                target_label=target.label,
                set_name=target.set_name,
                action="deploy",
                scenario="success",
                image_tag="bbbbbbb",
                status="running",
                created_at=int(time.time() * 1000),
            )
        )
        session.commit()
    restarted = create_app(web.settings, FakeGitHub())
    with TestClient(restarted, base_url=ORIGIN) as browser:
        browser.cookies.update(web.client.cookies)
        target = browser.get(web.endpoint).json()["target"]
        assert (
            target["state"] == "not_deployed" and not target["checked"] and target["versions"] == []
        )
        assert target["active_job_id"] is None
        assert browser.get(web.endpoint + "/jobs/" + deployed["id"]).json()["status"] == "succeeded"
        abandoned = browser.get(web.endpoint + "/jobs/" + abandoned_id).json()
        assert (
            abandoned["status"] == "interrupted"
            and abandoned["result"]["error"]["code"] == "worker_restarted"
        )
        with restarted.state.sessions() as session:
            assert session.get(MockDeployment, web.project["id"]).adapter_env_id == stable_id


def test_second_mock_worker_is_rejected_without_resetting_running_state(mock_web):
    select_target(mock_web)
    run(mock_web)
    with pytest.raises(RuntimeError, match="one Service process"):
        with TestClient(create_app(mock_web.settings, FakeGitHub()), base_url=ORIGIN):
            pass
    assert current(mock_web)["checked"]


def test_mock_requires_explicit_development_configuration(mock_web, monkeypatch):
    with pytest.raises(ValueError, match="only available in development"):
        replace(mock_web.settings, production=True)
    with pytest.raises(ValueError):
        replace(mock_web.settings, deployment_mode="anything")
    with pytest.raises(ValueError):
        replace(mock_web.settings, mock_step_delay=3)
    offline = create_app(replace(mock_web.settings, deployment_mode="unavailable"), FakeGitHub())
    with TestClient(offline, base_url=ORIGIN) as browser:
        browser.cookies.update(mock_web.client.cookies)
        assert browser.get(mock_web.endpoint).status_code == 503
        assert offline.state.mock_runner is None
    monkeypatch.setenv("APP_DEMO", "true")
    monkeypatch.setenv("APP_DEPLOYMENT_MODE", "mock")
    monkeypatch.setenv("APP_MOCK_STEP_DELAY", "0.1")
    settings = Settings.from_env()
    assert settings.deployment_mode == "mock" and settings.mock_step_delay == 0.1


def test_diagnostic_redaction_and_unknown_exception_do_not_leak(mock_web):
    web = mock_web
    select_target(web)
    sentinel = "ghp_" + "s" * 30

    class Noisy(MockAdapter):
        def check(self, env, log):
            log(LogEvent(message=sentinel, data={"private": "internal-value"}))
            raise RuntimeError(sentinel)

    web.app.state.mock_runner.adapters.clear()
    web.app.state.mock_runner.adapter_factory = Noisy
    job = run(web)
    assert job["status"] == "failed" and job["result"]["error"]["code"] == "adapter_error"
    assert sentinel not in json.dumps(job) and "internal-value" not in json.dumps(job)
    with web.app.state.sessions() as session:
        stored = session.get(MockJob, job["id"])
        assert sentinel not in stored.logs_json + stored.result_json

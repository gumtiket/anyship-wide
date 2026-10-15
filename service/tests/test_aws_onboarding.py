"""실제 인증·세션 처리와 가짜 AWS 어댑터로 서비스 API를 확인한다."""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.app import create_app
from app.aws_adapter import AwsCheckError, AwsIdentity
from app.config import Settings
from app.db import AwsEnvironment, Base, LoginSession, Membership
from tests.test_web import ORIGIN, FakeGitHub, oauth_start

ENDPOINT = "/api/aws/environments"
ROLE = "arn:aws:iam::123456789012:role/deploy-service-role"
SERVICE_ROLE = "arn:aws:iam::999999999999:role/service-server"
TEMPLATE = "https://example.s3.ap-northeast-2.amazonaws.com/onboarding.yaml?versionId=one+two&x=1"


class FakeAWS:
    def __init__(self):
        self.calls = []
        self.error = None
        self.account_id = "123456789012"
        self.hook = lambda: None

    def check(self, **values):
        self.calls.append(values)
        self.hook()
        if self.error:
            raise self.error
        return AwsIdentity(self.account_id)


def authenticate(client):
    state = oauth_start(client)
    client.get("/api/auth/github/callback", params={"state": state, "code": "code"})
    return {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/me").json()["csrf_token"]}


@pytest.fixture
def aws_web(database_url):
    settings = Settings(
        database_url=database_url,
        token_key=Fernet.generate_key().decode(),
        github_client_id="test",
        github_client_secret="test",
        github_app_slug="test",
        aws_template_url=TEMPLATE,
        aws_service_role_arn=SERVICE_ROLE,
        aws_regions=("ap-northeast-2", "us-east-1"),
    )
    adapter = FakeAWS()
    app = create_app(settings, FakeGitHub(), aws_adapter=adapter)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=ORIGIN) as client:
        yield app, client, adapter, authenticate(client), settings


def create(client, headers, **changes):
    return client.post(
        ENDPOINT,
        headers=headers,
        json={
            "request_id": str(uuid.uuid4()),
            "name": "운영 AWS",
            "region": "ap-northeast-2",
            **changes,
        },
    )


def verify(client, headers, identifier, role=ROLE, **changes):
    return client.post(
        f"{ENDPOINT}/{identifier}/verify", headers=headers, json={"role_arn": role, **changes}
    )


def save_role(client, headers, identifier, role=ROLE, **changes):
    return client.post(
        f"{ENDPOINT}/{identifier}/role", headers=headers, json={"role_arn": role, **changes}
    )


def test_url_creation_retries_and_configuration_snapshot(aws_web):
    app, client, adapter, headers, settings = aws_web
    request_id = str(uuid.uuid4())
    response = create(client, headers, request_id=request_id)
    assert response.status_code == 201
    row = response.json()
    assert (
        row["status"] == "PENDING" and row["role_arn"] is None and row["submitted_role_arn"] is None
    )
    parsed = urlsplit(row["cloudformation_url"])
    assert parsed.hostname == "ap-northeast-2.console.aws.amazon.com"
    params = parse_qs(parsed.fragment.split("?", 1)[1])
    assert params["templateURL"] == [TEMPLATE]
    assert params["param_ServiceRoleArn"] == [SERVICE_ROLE]
    assert params["param_RoleName"] == ["deploy-service-role"]
    assert params["stackName"] == [row["stack_name"]]
    with app.state.sessions() as session:
        stored = session.get(AwsEnvironment, row["id"])
        assert params["param_ExternalId"] == [stored.external_id]
        assert len(stored.external_id) == 64
    repeated = create(client, headers, request_id=request_id)
    assert repeated.status_code == 200 and repeated.json() == row
    assert create(client, headers, request_id=request_id, region="us-east-1").status_code == 409
    another = create(client, headers).json()
    assert another["stack_name"] != row["stack_name"]
    assert another["cloudformation_url"] != row["cloudformation_url"]
    assert not adapter.calls
    assert len(client.get(ENDPOINT).json()) == 2
    assert len(client.get(ENDPOINT + "?limit=1&offset=1").json()) == 1
    assert client.get("/api/config").json()["aws_regions"] == list(settings.aws_regions)
    # 재시작이나 설정 변경이 이미 발급한 신뢰 계약을 바꾸면 안 된다.
    restarted = create_app(
        replace(settings, aws_template_url=TEMPLATE + "2"), FakeGitHub(), aws_adapter=adapter
    )
    with TestClient(restarted, base_url=ORIGIN) as returning:
        returning.cookies.update(client.cookies)
        assert returning.get(ENDPOINT + "/" + row["id"]).json() == row


def test_verified_persistence_idempotence_and_relogin(aws_web):
    app, client, adapter, headers, _ = aws_web
    row = create(client, headers).json()
    with app.state.sessions() as session:
        external_id = session.get(AwsEnvironment, row["id"]).external_id
    response = verify(client, headers, row["id"])
    assert response.status_code == 200
    saved = response.json()
    assert saved["status"] == "CONNECTED"
    assert saved["role_arn"] == ROLE and saved["aws_account_id"] == "123456789012"
    assert saved["submitted_role_arn"] == ROLE
    assert saved["verified_at"] and saved["cloudformation_url"] is None
    assert adapter.calls == [
        {"role_arn": ROLE, "external_id": external_id, "region": "ap-northeast-2"}
    ]
    assert verify(client, headers, row["id"]).json() == saved and len(adapter.calls) == 1
    assert verify(client, headers, row["id"], ROLE + "other").status_code == 409
    assert save_role(client, headers, row["id"]).json() == saved
    assert save_role(client, headers, row["id"], ROLE + "other").status_code == 409
    client.post("/api/auth/logout", headers=headers)
    authenticate(client)
    assert client.get(ENDPOINT + "/" + row["id"]).json() == saved
    with app.state.sessions() as session:
        stored = session.get(AwsEnvironment, row["id"])
        assert stored.external_id == external_id and stored.verification_token == ""


@pytest.mark.parametrize("previous_error", [None, "service_credentials_unavailable"])
def test_without_adapter_saves_waits_and_resumes_same_registration(
    aws_web, monkeypatch, previous_error
):
    import boto3

    def forbid_aws(*args, **kwargs):
        pytest.fail("Service must not use AWS without an explicitly supplied adapter")

    monkeypatch.setattr(boto3, "Session", forbid_aws)
    app, client, adapter, headers, settings = aws_web
    waiting_app = create_app(settings, FakeGitHub())
    with TestClient(waiting_app, base_url=ORIGIN) as waiting:
        waiting.cookies.update(client.cookies)
        config = waiting.get("/api/config").json()
        assert config["aws_available"] and not config["aws_verification_available"]
        row = create(waiting, headers).json()
        assert row["cloudformation_url"]
        with app.state.sessions() as session:
            stored = session.get(AwsEnvironment, row["id"])
            external_id = stored.external_id
            if previous_error:
                stored.status = "FAILED"
                stored.error_code = previous_error
                session.commit()
        if previous_error:
            assert save_role(waiting, headers, row["id"]).status_code == 200
        for _ in range(2):
            response = verify(waiting, headers, row["id"])
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "aws_adapter_unavailable"
            pending = waiting.get(ENDPOINT + "/" + row["id"]).json()
            assert pending["status"] == "PENDING" and pending["submitted_role_arn"] == ROLE
            assert pending["role_arn"] is None and pending["aws_account_id"] is None
            assert pending["verified_at"] is None and pending["error_code"] is None
            assert pending["cloudformation_url"] == row["cloudformation_url"]
        assert not adapter.calls

    # 완성된 어댑터를 연결해도 저장된 같은 신뢰 계약을 쓴다.
    assert client.get("/api/config").json()["aws_verification_available"]
    verified = verify(client, headers, row["id"]).json()
    assert verified["status"] == "CONNECTED" and verified["role_arn"] == ROLE
    assert adapter.calls == [
        {"role_arn": ROLE, "external_id": external_id, "region": row["region"]}
    ]
    disconnected_app = create_app(settings, FakeGitHub())
    with TestClient(disconnected_app, base_url=ORIGIN) as disconnected:
        disconnected.cookies.update(client.cookies)
        assert verify(disconnected, headers, row["id"]).json() == verified


@pytest.mark.parametrize(
    "state, code, status",
    [
        ("VERIFYING", "verification_in_progress", 409),
        ("EXPIRED", "request_expired", 410),
    ],
)
def test_missing_adapter_respects_auth_expiry_and_active_verification(aws_web, state, code, status):
    app, client, _, headers, settings = aws_web
    row = create(client, headers).json()
    with app.state.sessions() as session:
        stored = session.get(AwsEnvironment, row["id"])
        stored.status = state
        stored.verification_token = "existing-verification"
        stored.lease_until = int(time.time()) + 60
        stored.submitted_role_arn = ROLE
        if state == "EXPIRED":
            stored.expires_at = int(time.time()) - 1
        session.commit()
    waiting_app = create_app(settings, FakeGitHub())
    with TestClient(waiting_app, base_url=ORIGIN) as waiting:
        assert verify(waiting, headers, row["id"]).status_code == 401
        waiting.cookies.update(client.cookies)
        assert verify(waiting, {}, row["id"]).status_code == 403
        response = verify(waiting, headers, row["id"], ROLE + "changed")
        assert response.status_code == status and response.json()["detail"]["code"] == code
    with app.state.sessions() as session:
        stored = session.get(AwsEnvironment, row["id"])
        assert stored.status == state and stored.submitted_role_arn == ROLE
        assert stored.verification_token == "existing-verification"


def test_access_control_and_client_cannot_override_server_values(aws_web):
    app, client, adapter, headers, settings = aws_web
    row = create(client, headers).json()
    assert create(client, {}).status_code == 403
    assert verify(client, {}, row["id"]).status_code == 403
    assert save_role(client, {}, row["id"]).status_code == 403
    assert (
        verify(client, {**headers, "Origin": "https://elsewhere.invalid"}, row["id"]).status_code
        == 403
    )
    assert (
        save_role(client, {**headers, "Origin": "https://elsewhere.invalid"}, row["id"]).status_code
        == 403
    )
    assert create(client, headers, external_id="forged").status_code == 422
    assert verify(client, headers, row["id"], external_id="forged").status_code == 422
    assert save_role(client, headers, row["id"], external_id="forged").status_code == 422
    other_gateway = FakeGitHub()
    other_gateway.profile = lambda token: {"id": 43, "login": "other", "name": "Other"}
    other_app = create_app(settings, other_gateway, aws_adapter=adapter)
    with TestClient(other_app, base_url=ORIGIN) as other:
        assert other.get(ENDPOINT).status_code == 401
        assert save_role(other, headers, row["id"]).status_code == 401
        other_headers = authenticate(other)
        assert other.get(ENDPOINT).json() == []
        assert other.get(ENDPOINT + "/" + row["id"]).status_code == 404
        assert verify(other, other_headers, row["id"]).status_code == 404
        assert save_role(other, other_headers, row["id"]).status_code == 404
        # 같은 워크스페이스 구성원이어도 다른 사용자의 요청에는 접근할 수 없다.
        with app.state.sessions() as session:
            original = session.get(AwsEnvironment, row["id"])
            other_login = session.scalar(
                select(LoginSession).where(LoginSession.csrf == other_headers["X-CSRF-Token"])
            )
            session.add(
                Membership(
                    user_id=other_login.user_id, workspace_id=original.workspace_id, role="owner"
                )
            )
            other_login.workspace_id = original.workspace_id
            session.commit()
        assert other.get(ENDPOINT).json() == []
        assert verify(other, other_headers, row["id"]).status_code == 404
        assert save_role(other, other_headers, row["id"]).status_code == 404
    assert not adapter.calls


@pytest.mark.parametrize(
    "role",
    [
        "bad",
        "arn:aws:iam::123456789012:user/name",
        "arn:aws:sts::123456789012:assumed-role/name/session",
        "arn:aws-cn:iam::123456789012:role/name",
    ],
)
def test_invalid_roles_never_reach_aws(aws_web, role):
    _, client, adapter, headers, _ = aws_web
    row = create(client, headers).json()
    assert verify(client, headers, row["id"], role).status_code == 422
    assert save_role(client, headers, row["id"], role).status_code == 422
    assert client.get(ENDPOINT + "/" + row["id"]).json()["submitted_role_arn"] is None
    assert not adapter.calls


@pytest.mark.parametrize(
    "error",
    [
        AwsCheckError("access_denied"),
        AwsCheckError("external_id_not_required"),
        AwsCheckError("service_credentials_unavailable"),
        RuntimeError("SECRET-ACCESS-KEY SESSION-TOKEN"),
    ],
)
def test_failure_is_safe_and_can_retry(aws_web, error, caplog):
    app, client, adapter, headers, _ = aws_web
    row = create(client, headers).json()
    adapter.error = error

    def assert_input_committed():
        with app.state.sessions() as session:
            stored = session.get(AwsEnvironment, row["id"])
            assert stored.submitted_role_arn == ROLE and stored.role_arn is None
            assert stored.status == "VERIFYING"

    adapter.hook = assert_input_committed
    response = verify(client, headers, row["id"])
    assert response.status_code in (403, 422, 503)
    assert "SECRET-ACCESS-KEY" not in response.text + caplog.text
    failed = client.get(ENDPOINT + "/" + row["id"]).json()
    assert failed["status"] == "FAILED" and failed["role_arn"] is None and failed["retryable"]
    assert failed["submitted_role_arn"] == ROLE and failed["verified_at"] is None
    assert failed["cloudformation_url"] == row["cloudformation_url"]
    with app.state.sessions() as session:
        assert session.get(AwsEnvironment, row["id"]).aws_account_id is None
    adapter.error = None
    assert verify(client, headers, row["id"]).status_code == 200


def test_save_role_without_aws_setup_survives_restart_and_relogin(aws_web):
    app, client, adapter, headers, settings = aws_web
    row = create(client, headers).json()
    with app.state.sessions() as session:
        external_id = session.get(AwsEnvironment, row["id"]).external_id
    settings = replace(settings, aws_template_url="", aws_service_role_arn="", aws_regions=())
    for attempt in range(2):
        restarted = create_app(settings, FakeGitHub(), aws_adapter=adapter)
        with TestClient(restarted, base_url=ORIGIN) as browser:
            returning_headers = authenticate(browser)
            if attempt == 0:
                response = save_role(browser, returning_headers, row["id"])
                assert response.status_code == 200
                saved = response.json()
                assert saved["submitted_role_arn"] == ROLE and saved["status"] == "PENDING"
                assert saved["role_arn"] is None and saved["aws_account_id"] is None
                assert saved["verified_at"] is None
            assert browser.get(ENDPOINT + "/" + row["id"]).json() == saved
            assert browser.get(ENDPOINT).json()[0]["submitted_role_arn"] == ROLE
            assert save_role(browser, returning_headers, row["id"]).json() == saved
    with app.state.sessions() as session:
        stored = session.get(AwsEnvironment, row["id"])
        assert stored.submitted_role_arn == ROLE and stored.external_id == external_id
    assert not adapter.calls


def test_edit_saved_role_after_failed_check_then_verify(aws_web):
    app, client, adapter, headers, _ = aws_web
    row = create(client, headers).json()
    adapter.error = AwsCheckError("service_credentials_unavailable")
    assert verify(client, headers, row["id"]).status_code == 503
    replacement = ROLE + "-corrected"
    saved = save_role(client, headers, row["id"], replacement).json()
    assert saved["submitted_role_arn"] == replacement
    assert saved["status"] == "PENDING" and saved["error_code"] is None
    assert saved["role_arn"] is None and saved["verified_at"] is None
    assert len(adapter.calls) == 1
    adapter.error = None
    connected = verify(client, headers, row["id"], replacement).json()
    assert connected["status"] == "CONNECTED" and connected["role_arn"] == replacement


def test_expiry_and_account_mismatch_do_not_connect(aws_web):
    app, client, adapter, headers, _ = aws_web
    row = create(client, headers).json()
    adapter.account_id = "000000000000"
    assert verify(client, headers, row["id"]).json()["detail"]["code"] == "account_mismatch"
    with app.state.sessions() as session:
        session.get(AwsEnvironment, row["id"]).expires_at = int(time.time()) - 1
        session.commit()
    expired = client.get(ENDPOINT + "/" + row["id"]).json()
    assert expired["status"] == "EXPIRED" and expired["cloudformation_url"] is None
    assert verify(client, headers, row["id"]).status_code == 410
    assert save_role(client, headers, row["id"], ROLE + "other").status_code == 410
    assert client.get(ENDPOINT + "/" + row["id"]).json()["submitted_role_arn"] == ROLE
    assert len(adapter.calls) == 1


@pytest.mark.parametrize("error", [None, AwsCheckError("access_denied")])
def test_lease_recovery_and_late_result_cannot_overwrite_new_attempt(aws_web, error):
    app, client, adapter, headers, _ = aws_web
    row = create(client, headers).json()
    with app.state.sessions() as session:
        stored = session.get(AwsEnvironment, row["id"])
        stored.status, stored.lease_until, stored.verification_token = (
            "VERIFYING",
            int(time.time()) - 1,
            "old",
        )
        session.commit()
    assert client.get(ENDPOINT + "/" + row["id"]).json()["error_code"] == "verification_interrupted"

    def supersede():
        with app.state.sessions() as session:
            session.execute(
                update(AwsEnvironment)
                .where(AwsEnvironment.id == row["id"])
                .values(verification_token="new")
            )
            session.commit()

    adapter.hook = supersede
    adapter.error = error
    response = verify(client, headers, row["id"])
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "verification_superseded"
    )
    with app.state.sessions() as session:
        stored = session.get(AwsEnvironment, row["id"])
        assert stored.role_arn is None and stored.verification_token == "new"


def test_concurrent_verification_calls_adapter_once(aws_web):
    app, client, adapter, headers, _ = aws_web
    row = create(client, headers).json()
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(10)

    adapter.hook = block
    with TestClient(app, base_url=ORIGIN) as second, ThreadPoolExecutor(max_workers=1) as pool:
        second.cookies.update(client.cookies)
        first = pool.submit(verify, client, headers, row["id"])
        try:
            assert entered.wait(10)
            pending = second.get(ENDPOINT + "/" + row["id"]).json()
            assert pending["status"] == "VERIFYING" and pending["submitted_role_arn"] == ROLE
            assert save_role(second, headers, row["id"], ROLE + "other").status_code == 409
            assert verify(second, headers, row["id"], ROLE + "other").status_code == 409
            assert second.get(ENDPOINT + "/" + row["id"]).json()["submitted_role_arn"] == ROLE
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 200
    assert len(adapter.calls) == 1


def test_same_verified_role_cannot_be_registered_twice(aws_web):
    app, client, _, headers, _ = aws_web
    first = create(client, headers).json()
    second = create(client, headers).json()
    # 확인되지 않은 입력이 역할을 선점하거나 다른 연결을 막으면 안 된다.
    assert save_role(client, headers, first["id"]).status_code == 200
    assert save_role(client, headers, second["id"]).status_code == 200
    assert verify(client, headers, first["id"]).status_code == 200
    response = verify(client, headers, second["id"])
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "role_already_registered"
    )
    with app.state.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AwsEnvironment)
                .where(AwsEnvironment.status == "CONNECTED")
            )
            == 1
        )
        assert session.get(AwsEnvironment, second["id"]).role_arn is None
        assert session.get(AwsEnvironment, second["id"]).submitted_role_arn == ROLE


@pytest.mark.parametrize("error", [None, AwsCheckError("access_denied")])
def test_saving_after_lease_expiry_rejects_late_verification_result(aws_web, error):
    app, client, adapter, headers, _ = aws_web
    row = create(client, headers).json()
    replacement = ROLE + "-corrected"
    with TestClient(app, base_url=ORIGIN) as second:
        second.cookies.update(client.cookies)

        def save_new_input():
            with app.state.sessions() as session:
                session.get(AwsEnvironment, row["id"]).lease_until = int(time.time()) - 1
                session.commit()
            assert save_role(second, headers, row["id"], replacement).status_code == 200

        adapter.hook = save_new_input
        adapter.error = error
        response = verify(client, headers, row["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "verification_superseded"
    saved = client.get(ENDPOINT + "/" + row["id"]).json()
    assert saved["status"] == "PENDING" and saved["submitted_role_arn"] == replacement
    assert saved["role_arn"] is None and saved["verified_at"] is None


def test_configuration_missing_changed_and_unsupported_regions(aws_web):
    _, client, adapter, headers, settings = aws_web
    assert create(client, headers, region="eu-west-1").status_code == 422
    assert create(client, headers, name="  ").status_code == 422
    row = create(client, headers).json()
    for configured, code in (
        (replace(settings, aws_template_url=""), "aws_not_configured"),
        (replace(settings, aws_service_role_arn=SERVICE_ROLE + "new"), "configuration_changed"),
    ):
        other_app = create_app(configured, FakeGitHub(), aws_adapter=adapter)
        with TestClient(other_app, base_url=ORIGIN) as other:
            other.cookies.update(client.cookies)
            assert verify(other, headers, row["id"]).json()["detail"]["code"] == code
            if code == "aws_not_configured":
                assert create(other, headers).status_code == 503
                config = other.get("/api/config").json()
                assert not config["aws_available"]
                assert config["aws_regions"] == list(settings.aws_regions)
                assert config["aws_setup_issues"] == ["template"]
    assert not adapter.calls


@pytest.mark.parametrize(
    "changes, issues",
    [
        ({"aws_template_url": "", "aws_service_role_arn": ""}, ["template", "service_role"]),
        ({"aws_regions": ()}, ["regions"]),
        ({"demo": True}, ["live_mode"]),
        ({}, []),
    ],
)
def test_config_reports_setup_gaps_without_exposing_values(aws_web, changes, issues):
    _, _, adapter, _, settings = aws_web
    app = create_app(replace(settings, **changes), FakeGitHub(), aws_adapter=adapter)
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get("/api/config")
        config = response.json()
        assert config["aws_setup_issues"] == issues
        assert config["aws_available"] == (not issues)
        assert config["aws_verification_available"] == (not issues)
        assert config["aws_regions"] == list(changes.get("aws_regions", settings.aws_regions))
        assert "no-store" in response.headers["Cache-Control"]
        for value in (TEMPLATE, SERVICE_ROLE, settings.token_key):
            assert value not in response.text
    assert not adapter.calls


@pytest.mark.parametrize("error", [None, AwsCheckError("access_denied")])
def test_request_expiring_during_validation_is_not_saved(aws_web, monkeypatch, error):
    from app import aws_onboarding

    _, client, adapter, headers, _ = aws_web
    row = create(client, headers).json()
    adapter.hook = lambda: monkeypatch.setattr(
        aws_onboarding, "time", SimpleNamespace(time=lambda: row["expires_at"])
    )
    adapter.error = error
    response = verify(client, headers, row["id"])
    assert response.status_code == 410 and not response.json()["detail"]["retryable"]
    expired = client.get(ENDPOINT + "/" + row["id"]).json()
    assert expired["role_arn"] is None and expired["status"] == "EXPIRED"
    assert expired["submitted_role_arn"] == ROLE


def test_unique_role_name_option_and_demo_registration_disabled(aws_web):
    _, client, adapter, headers, settings = aws_web
    app = create_app(
        replace(settings, aws_role_name="deploy-service-role-{id}"),
        FakeGitHub(),
        aws_adapter=adapter,
    )
    with TestClient(app, base_url=ORIGIN) as alternate:
        alternate.cookies.update(client.cookies)
        row = create(alternate, headers).json()
        assert row["role_name"] == "deploy-service-role-" + uuid.UUID(row["id"]).hex
    demo = create_app(replace(settings, demo=True), aws_adapter=adapter)
    with TestClient(demo, base_url=ORIGIN) as browser:
        browser.post("/api/auth/demo", headers={"Origin": ORIGIN})
        csrf = browser.get("/api/me").json()["csrf_token"]
        assert create(browser, {"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 503
    assert not adapter.calls


def test_concurrent_create_returns_one_request_and_external_id(aws_web):
    app, client, _, headers, _ = aws_web
    request_id = str(uuid.uuid4())
    gate = threading.Barrier(2)

    def start(browser):
        gate.wait(timeout=10)
        return create(browser, headers, request_id=request_id)

    with TestClient(app, base_url=ORIGIN) as second, ThreadPoolExecutor(max_workers=2) as pool:
        second.cookies.update(client.cookies)
        first, another = pool.submit(start, client), pool.submit(start, second)
        results = [first.result(timeout=15), another.result(timeout=15)]
    assert sorted(result.status_code for result in results) == [200, 201]
    assert results[0].json() == results[1].json()
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AwsEnvironment)) == 1

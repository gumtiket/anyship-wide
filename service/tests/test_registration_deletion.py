"""Delete only Service registrations; preserve remote resources and active work."""

import time
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.app import create_app
from app.db import AwsEnvironment, CodeChange, DemoChange, LoginSession, Membership, Project
from tests.test_aws_onboarding import ENDPOINT, authenticate, create, save_role, verify
from tests.test_real_workflow import NAME, login, prepare, publish
from tests.test_web import ORIGIN, FakeGitHub


@pytest.mark.parametrize(
    "state", ["PENDING", "FAILED", "EXPIRED", "CONNECTED", "stale_verification"]
)
def test_delete_environment_all_inactive_states_without_aws(aws_web, state):
    app, client, adapter, headers, settings = aws_web
    row = create(client, headers).json()
    retained = create(client, headers).json()
    assert save_role(client, headers, row["id"]).status_code == 200
    if state == "CONNECTED":
        assert verify(client, headers, row["id"]).status_code == 200
    elif state != "PENDING":
        with app.state.sessions() as session:
            stored = session.get(AwsEnvironment, row["id"])
            stored.status = "VERIFYING" if state == "stale_verification" else state
            stored.lease_until = int(time.time()) - 1
            if state == "EXPIRED":
                stored.expires_at = int(time.time()) - 1
            session.commit()
    calls = len(adapter.calls)
    # Removing a registration needs neither AWS configuration nor an adapter.
    offline = create_app(
        replace(settings, aws_template_url="", aws_service_role_arn=""), FakeGitHub()
    )
    with TestClient(offline, base_url=ORIGIN) as browser:
        browser.cookies.update(client.cookies)
        response = browser.delete(f"{ENDPOINT}/{row['id']}", headers=headers)
        assert response.status_code == 204 and response.content == b""
        assert browser.get(f"{ENDPOINT}/{row['id']}").status_code == 404
        assert [item["id"] for item in browser.get(ENDPOINT).json()] == [retained["id"]]
        assert browser.delete(f"{ENDPOINT}/{row['id']}", headers=headers).status_code == 404
    assert len(adapter.calls) == calls
    with app.state.sessions() as session:
        assert session.get(AwsEnvironment, row["id"]) is None
        assert session.get(AwsEnvironment, retained["id"]) is not None


def test_environment_deletion_auth_csrf_and_owner_isolation(aws_web):
    app, client, _, headers, settings = aws_web
    row = create(client, headers).json()
    endpoint = f"{ENDPOINT}/{row['id']}"
    assert client.delete(endpoint).status_code == 403
    assert (
        client.delete(endpoint, headers={**headers, "Origin": "https://evil.invalid"}).status_code
        == 403
    )
    assert client.delete(endpoint, headers={**headers, "X-CSRF-Token": "wrong"}).status_code == 403
    gateway = FakeGitHub()
    gateway.profile = lambda token: {"id": 99, "login": "other", "name": "Other"}
    with TestClient(create_app(settings, gateway), base_url=ORIGIN) as other:
        assert other.delete(endpoint, headers=headers).status_code == 401
        other_headers = authenticate(other)
        assert other.delete(endpoint, headers=other_headers).status_code == 404
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
        assert other.delete(endpoint, headers=other_headers).status_code == 404
    assert client.get(endpoint).status_code == 200


def test_deletion_blocked_during_check_and_late_result_cannot_restore_environment(aws_web):
    app, client, adapter, headers, _ = aws_web
    row = create(client, headers).json()
    endpoint = f"{ENDPOINT}/{row['id']}"

    def finish_after_deletion():
        assert client.delete(endpoint, headers=headers).status_code == 409
        # A timed-out check may still finish after its registration is removed.
        with app.state.sessions() as session:
            session.execute(
                update(AwsEnvironment)
                .where(AwsEnvironment.id == row["id"])
                .values(lease_until=int(time.time()) - 1)
            )
            session.commit()
        assert client.delete(endpoint, headers=headers).status_code == 204

    adapter.hook = finish_after_deletion
    response = verify(client, headers, row["id"])
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "verification_superseded"
    )
    assert client.get(endpoint).status_code == 404


@pytest.mark.parametrize("published", [False, True])
def test_delete_repository_cleans_local_history_and_preserves_github(real, published):
    app, client, remote, headers, endpoint = real
    change = prepare(real)
    if published:
        assert publish(real, change).status_code == 200
    project_id = endpoint.rsplit("/", 1)[1]
    with app.state.sessions() as session:
        original = session.get(Project, project_id)
        retained = Project(
            id=str(uuid.uuid4()),
            workspace_id=original.workspace_id,
            repository_id=987,
            installation_id=original.installation_id,
            full_name="owner/other",
            branch="main",
            base_sha=original.base_sha,
            created_by=original.created_by,
            created_at=1,
        )
        session.add(retained)
        demo = DemoChange(id=str(uuid.uuid4()), project_id=project_id, content="test")
        session.add(demo)
        session.commit()
        retained_id, demo_id = retained.id, demo.id
    calls, refs, prs = len(remote.calls), dict(remote.refs), list(remote.prs)
    remote.revoked = True  # Local deletion must work after GitHub access is revoked.
    response = client.delete(endpoint, headers=headers)
    assert response.status_code == 204 and response.content == b""
    assert client.get(endpoint).status_code == 404
    assert client.get(endpoint + "/changes").status_code == 404
    assert client.delete(endpoint, headers=headers).status_code == 404
    assert [p["id"] for p in client.get("/api/projects").json()] == [retained_id]
    assert len(remote.calls) == calls and remote.refs == refs and remote.prs == prs
    with app.state.sessions() as session:
        assert session.get(Project, project_id) is None
        assert session.get(CodeChange, change["id"]) is None
        assert session.get(DemoChange, demo_id) is None
    remote.revoked = False
    # The uniqueness constraint is released, so the original repository can be reconnected.
    response = client.post(
        "/api/projects",
        json={"repository_url": f"https://github.com/{NAME}", "branch": "main"},
        headers=headers,
    )
    assert response.status_code == 201 and response.json()["id"] != project_id


def test_repository_deletion_auth_csrf_and_workspace_isolation(real):
    app, client, remote, headers, endpoint = real
    assert client.delete(endpoint).status_code == 403
    assert client.delete(endpoint, headers={**headers, "X-CSRF-Token": "wrong"}).status_code == 403
    assert (
        client.delete(endpoint, headers={**headers, "Origin": "https://evil.invalid"}).status_code
        == 403
    )
    with TestClient(app, base_url=ORIGIN) as other:
        assert other.delete(endpoint, headers=headers).status_code == 401
        remote.user_id = 2
        other_headers = login(other)
        assert other.delete(endpoint, headers=other_headers).status_code == 404
    assert client.get(endpoint).status_code == 200


@pytest.mark.parametrize("busy_kind", ["real", "demo_apply", "demo_publish"])
def test_busy_repository_delete_rolls_back_all_local_cleanup(real, busy_kind):
    app, client, remote, headers, endpoint = real
    change = prepare(real)
    project_id = endpoint.rsplit("/", 1)[1]
    with app.state.sessions() as session:
        stored = session.get(CodeChange, change["id"])
        if busy_kind == "real":
            stored.status = "publishing"
            stored.lease_until = int(time.time()) + 600
        demo = DemoChange(
            id=str(uuid.uuid4()),
            project_id=project_id,
            content="test",
            status={"real": "proposed", "demo_apply": "applying", "demo_publish": "publishing"}[
                busy_kind
            ],
        )
        session.add(demo)
        session.commit()
        demo_id = demo.id
    calls = len(remote.calls)
    assert client.delete(endpoint, headers=headers).status_code == 409
    assert len(remote.calls) == calls
    with app.state.sessions() as session:
        assert session.get(Project, project_id) is not None
        assert session.get(CodeChange, change["id"]) is not None
        assert session.get(DemoChange, demo_id) is not None
        session.get(CodeChange, change["id"]).lease_until = 0
        session.get(DemoChange, demo_id).status = "applied"
        session.commit()
    assert client.delete(endpoint, headers=headers).status_code == 204

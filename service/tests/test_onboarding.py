"""Onboarding authorization, state lifecycle, account isolation and recovery."""

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.app import create_app
from app.config import Settings
from app.db import Base, RepositoryConnection
from tests.test_real_workflow import NAME, ORIGIN, GitHubHTTP, login

ENDPOINT = "/api/github/connection"
URL = f"https://github.com/{NAME}"


@pytest.fixture
def onboarding(database_url, monkeypatch):
    remote = GitHubHTTP()
    with httpx.Client(transport=httpx.MockTransport(remote)) as transport:
        monkeypatch.setattr(httpx, "request", transport.request)
        app = create_app(
            Settings(
                database_url=database_url,
                token_key=Fernet.generate_key().decode(),
                github_client_id="test",
                github_client_secret="test",
                github_app_slug="anyship",
            )
        )
        Base.metadata.create_all(app.state.engine)
        with TestClient(app, base_url=ORIGIN) as client:
            yield app, client, remote, login(client)


def save(client, headers, url=URL):
    response = client.post(ENDPOINT, headers=headers, json={"repository_url": url})
    assert response.status_code == 200
    return response.json()


def check(client, headers, draft):
    return client.post(ENDPOINT + "/check", headers=headers, json={"connection_id": draft["id"]})


def authorize(client, headers, draft):
    response = client.post(
        ENDPOINT + "/authorize", headers=headers, json={"connection_id": draft["id"]}
    )
    assert response.status_code == 200
    url = urlsplit(response.json()["url"])
    assert url.scheme == "https" and url.netloc == "github.com"
    assert url.path == "/apps/anyship/installations/new"
    return parse_qs(url.query)["state"][0]


def returned(client, headers, state):
    return client.post(ENDPOINT + "/return", headers=headers, json={"state": state})


def test_install_return_checks_access_and_consumes_state(onboarding):
    app, client, remote, headers = onboarding
    remote.repo_selected = False
    draft = save(client, headers, URL + ".git")
    assert draft["repository_url"] == URL
    assert check(client, headers, draft).json()["status"] == "authorization_required"
    state = authorize(client, headers, draft)
    with app.state.sessions() as session:
        row = session.scalar(select(RepositoryConnection))
        assert row.install_state_hash != state and len(row.install_state_hash) == 64
    assert client.get(ENDPOINT).json()["awaiting_approval"] is True
    remote.repo_selected = True
    result = returned(client, headers, state)
    assert result.status_code == 200
    assert result.json()["status"] == "ready"
    assert result.json()["repository"]["branches"][0]["name"] == "main"
    assert client.get(ENDPOINT).json()["awaiting_approval"] is False
    assert returned(client, headers, state).status_code == 409
    assert client.get("/api/projects").json() == []
    assert remote.writes == []


def test_other_user_cannot_read_or_consume_connection(onboarding):
    app, client, remote, headers = onboarding
    draft = save(client, headers)
    state = authorize(client, headers, draft)
    remote.user_id = 2
    with TestClient(app, base_url=ORIGIN) as other:
        second_headers = login(other)
        assert other.get(ENDPOINT).json() is None
        assert check(other, second_headers, draft).status_code == 409
        assert returned(other, second_headers, state).status_code == 409
        assert (
            other.request(
                "DELETE", ENDPOINT, headers=second_headers, json={"connection_id": draft["id"]}
            ).status_code
            == 409
        )
    assert returned(client, headers, state).json()["status"] == "ready"


def test_draft_survives_relogin_and_is_cleared_only_after_project_commit(onboarding):
    _, client, remote, headers = onboarding
    draft = save(client, headers)
    client.post("/api/auth/logout", headers=headers)
    assert client.get(ENDPOINT).status_code == 401
    headers = login(client)
    assert client.get(ENDPOINT).json()["id"] == draft["id"]
    assert check(client, headers, draft).json()["status"] == "ready"
    remote.writable = False
    payload = {"repository_url": URL, "branch": "main"}
    assert client.post("/api/projects", headers=headers, json=payload).status_code == 403
    assert client.get(ENDPOINT).json() is not None
    remote.writable = True
    project = client.post("/api/projects", headers=headers, json=payload)
    assert project.status_code == 201
    assert client.get(ENDPOINT).json() is None
    again = save(client, headers)
    result = check(client, headers, again).json()
    assert result["status"] == "connected" and result["project_id"] == project.json()["id"]
    assert client.post("/api/projects", headers=headers, json=payload).status_code == 409
    assert client.get(ENDPOINT).json()["id"] == again["id"]


@pytest.mark.parametrize(
    "attribute,value,status",
    [
        ("revoked", True, "authorization_required"),
        ("repo_selected", False, "authorization_required"),
        ("writable", False, "write_required"),
        ("archived", True, "archived"),
        ("suspended", True, "suspended"),
        ("permissions", {"contents": "read", "pull_requests": "write"}, "permissions_required"),
        ("empty", True, "empty_repository"),
    ],
)
def test_current_permissions_are_rechecked_even_after_valid_return(
    onboarding, attribute, value, status
):
    _, client, remote, headers = onboarding
    draft = save(client, headers)
    assert check(client, headers, draft).json()["status"] == "ready"
    state = authorize(client, headers, draft)
    setattr(remote, attribute, value)
    response = returned(client, headers, state)
    assert response.status_code == 200 and response.json()["status"] == status
    assert remote.writes == []


def test_forged_expired_and_superseded_return_cannot_change_connection(onboarding):
    app, client, _, headers = onboarding
    draft = save(client, headers)
    state = authorize(client, headers, draft)
    assert returned(client, headers, "forged").status_code == 409
    with app.state.sessions() as session:
        row = session.scalar(select(RepositoryConnection))
        row.install_expires_at = int(time.time()) - 1
        session.commit()
    assert returned(client, headers, state).status_code == 409
    assert check(client, headers, draft).json()["status"] == "ready"
    state = authorize(client, headers, draft)
    new_draft = save(client, headers)
    assert new_draft["id"] != draft["id"]
    assert returned(client, headers, state).status_code == 409
    assert check(client, headers, draft).status_code == 409
    assert check(client, headers, new_draft).json()["status"] == "ready"


def test_input_csrf_cancellation_and_expiry(onboarding):
    app, client, _, headers = onboarding
    draft = save(client, headers)
    for bad in (
        "https://attacker.invalid/owner/repo",
        "https://github.com/owner/repo/tree/main",
        "file:///secret",
    ):
        assert (
            client.post(ENDPOINT, headers=headers, json={"repository_url": bad}).status_code == 422
        )
    assert client.get(ENDPOINT).json()["id"] == draft["id"]
    for suffix, body in (
        ("", {"repository_url": URL}),
        ("/check", {"connection_id": draft["id"]}),
        ("/authorize", {"connection_id": draft["id"]}),
        ("/return", {"state": "anything"}),
    ):
        assert client.post(ENDPOINT + suffix, json=body).status_code == 403
    assert (
        client.request("DELETE", ENDPOINT, json={"connection_id": draft["id"]}).status_code == 403
    )
    assert (
        client.request(
            "DELETE", ENDPOINT, headers=headers, json={"connection_id": draft["id"]}
        ).status_code
        == 200
    )
    assert client.get(ENDPOINT).json() is None
    draft = save(client, headers)
    with app.state.sessions() as session:
        row = session.scalar(select(RepositoryConnection))
        row.expires_at = int(time.time()) - 1
        session.commit()
    assert client.get(ENDPOINT).json() is None
    assert check(client, headers, draft).status_code == 409


def test_unverified_install_redirect_only_opens_connection_form(onboarding):
    _, client, remote, _ = onboarding
    response = client.get("/api/github/install", follow_redirects=False)
    assert response.headers["location"] == "/#connect"
    assert remote.writes == []

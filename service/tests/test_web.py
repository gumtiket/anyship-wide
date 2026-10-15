import time
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.app import create_app
from app.config import Settings
from app.db import Base, LoginSession, OAuthAttempt
from app.github_api import DemoGitHub, GitHubFailure

ORIGIN = "http://localhost:8000"


@pytest.fixture
def web(tmp_path, database_url):
    app = create_app(
        Settings(demo=True, database_url=database_url, demo_workspaces=tmp_path / "demo-workspaces")
    )
    Base.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=ORIGIN) as client:
        yield app, client


def login(client):
    assert client.post("/api/auth/demo", headers={"Origin": ORIGIN}).status_code == 200
    me = client.get("/api/me").json()
    return {"Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"]}


def add(client, headers, **changes):
    payload = {"installation_id": 101, "repository_id": 1001, "branch": "main", **changes}
    return client.post("/api/projects", headers=headers, json=payload)


def test_demo_project_is_persisted_and_pins_branch(web):
    _, client = web
    headers = login(client)
    response = add(client, headers, branch="develop")
    assert response.status_code == 201
    assert response.json()["base_sha"] == "b" * 40
    assert client.get("/api/projects").json() == [response.json()]
    assert add(client, headers).status_code == 409
    assert client.get("/api/config").json()["ai_available"] is False


def test_another_user_cannot_read_project(web):
    app, first = web
    project = add(first, login(first)).json()
    with TestClient(app, base_url=ORIGIN) as second:
        login(second)
        assert second.get("/api/projects").json() == []
        assert second.get(f"/api/projects/{project['id']}").status_code == 404


def test_authentication_csrf_and_logout(web):
    app, client = web
    assert client.get("/api/projects").status_code == 401
    assert (
        client.post("/api/auth/demo", headers={"Origin": "https://attacker.invalid"}).status_code
        == 403
    )
    headers = login(client)
    assert add(client, {}).status_code == 403
    assert add(client, {**headers, "Origin": "https://attacker.invalid"}).status_code == 403
    assert add(client, {**headers, "X-CSRF-Token": "wrong"}).status_code == 403
    raw = client.cookies.get("app_session")
    with app.state.sessions() as session:
        stored = session.scalar(select(LoginSession))
        assert stored.id != raw
        assert stored.token_cipher != "demo"
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/me").status_code == 401
    client.cookies.set("app_session", raw)
    assert client.get("/api/me").status_code == 401


def test_access_revalidated_and_bad_selection_rejected(web):
    _, client = web
    headers = login(client)
    assert add(client, headers, installation_id=999).status_code == 404
    assert add(client, headers, repository_id=999).status_code == 404
    assert add(client, headers, branch="missing").status_code == 404
    assert client.get("/api/projects").json() == []


def test_expired_session_is_rejected(web):
    app, client = web
    login(client)
    with app.state.sessions() as session:
        stored = session.scalar(select(LoginSession))
        stored.expires_at = int(time.time()) - 1
        session.commit()
    assert client.get("/api/me").status_code == 401


class FakeGitHub(DemoGitHub):
    def __init__(self):
        self.exchange_calls = []
        self.revoked = False

    def exchange(self, settings, code, verifier):
        self.exchange_calls.append((code, verifier))
        return {"access_token": "sensitive-user-token", "expires_in": 3600}

    def profile(self, token):
        return {"id": 42, "login": "real-user", "name": "Real User"}

    def installations(self, token):
        if self.revoked:
            return []
        return super().installations(token)


@pytest.fixture
def real_web(database_url):
    gateway = FakeGitHub()
    settings = Settings(
        database_url=database_url,
        token_key=Fernet.generate_key().decode(),
        github_client_id="test",
        github_client_secret="secret",
        github_app_slug="test-app",
    )
    app = create_app(settings, gateway)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=ORIGIN) as client:
        yield app, client, gateway


def oauth_start(client):
    response = client.get("/api/auth/github/start", follow_redirects=False)
    assert response.status_code == 302
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert "HttpOnly" in response.headers["set-cookie"]
    return params["state"][0]


def test_oauth_binding_one_time_state_and_encrypted_token(real_web):
    app, client, gateway = real_web
    state = oauth_start(client)
    with TestClient(app, base_url=ORIGIN) as other:
        result = other.get(
            "/api/auth/github/callback",
            params={"state": state, "code": "code"},
            follow_redirects=False,
        )
        assert "auth_error" in result.headers["location"]
        assert gateway.exchange_calls == []
    result = client.get(
        "/api/auth/github/callback", params={"state": state, "code": "code"}, follow_redirects=False
    )
    assert result.headers["location"] == "/"
    assert client.get("/api/me").json()["user"]["login"] == "real-user"
    with app.state.sessions() as session:
        stored = session.scalar(select(LoginSession))
        assert "sensitive-user-token" not in stored.token_cipher
        assert session.scalar(select(OAuthAttempt)) is None
    client.get("/api/auth/github/callback", params={"state": state, "code": "code"})
    assert len(gateway.exchange_calls) == 1
    assert client.post("/api/auth/demo", headers={"Origin": ORIGIN}).status_code == 404


def test_revoked_installation_cannot_register_project(real_web):
    _, client, gateway = real_web
    state = oauth_start(client)
    client.get("/api/auth/github/callback", params={"state": state, "code": "code"})
    me = client.get("/api/me").json()
    gateway.revoked = True
    assert add(client, {"Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"]}).status_code == 404


def test_real_login_reuses_workspace_and_expired_oauth_is_rejected(real_web):
    app, client, gateway = real_web
    state = oauth_start(client)
    with app.state.sessions() as session:
        attempt = session.scalar(select(OAuthAttempt))
        attempt.expires_at = int(time.time()) - 1
        session.commit()
    failed = client.get(
        "/api/auth/github/callback", params={"state": state, "code": "code"}, follow_redirects=False
    )
    assert "auth_error" in failed.headers["location"]
    assert not gateway.exchange_calls
    state = oauth_start(client)
    client.get("/api/auth/github/callback", params={"state": state, "code": "code"})
    me = client.get("/api/me").json()
    project = add(client, {"Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"]}).json()
    state = oauth_start(client)
    client.get("/api/auth/github/callback", params={"state": state, "code": "second-code"})
    assert client.get("/api/me").json()["workspace"] == me["workspace"]
    assert client.get("/api/projects").json() == [project]


def test_production_cannot_enable_demo_or_http():
    with pytest.raises(ValueError):
        Settings(demo=True, production=True)
    with pytest.raises(ValueError):
        Settings(production=True, token_key=Fernet.generate_key().decode())


def test_login_reports_safe_github_error_code(real_web, monkeypatch, caplog):
    _, client, gateway = real_web

    def fail_exchange(*args):
        raise GitHubFailure(401, "incorrect_client_credentials")

    monkeypatch.setattr(gateway, "exchange", fail_exchange)
    state = oauth_start(client)
    result = client.get(
        "/api/auth/github/callback",
        params={"state": state, "code": "sensitive-code"},
        follow_redirects=False,
    )
    assert result.headers["location"] == "/?auth_error=incorrect_client_credentials"
    assert "incorrect_client_credentials" in caplog.text
    assert "sensitive-code" not in caplog.text
    assert 'app_oauth=""' in result.headers["set-cookie"]


def test_unknown_github_error_is_not_reflected(real_web, monkeypatch, caplog):
    _, client, gateway = real_web

    def fail_exchange(*args):
        raise GitHubFailure(401, "arbitrary-sensitive-upstream-detail")

    monkeypatch.setattr(gateway, "exchange", fail_exchange)
    state = oauth_start(client)
    result = client.get(
        "/api/auth/github/callback", params={"state": state, "code": "code"}, follow_redirects=False
    )
    assert result.headers["location"] == "/?auth_error=github_error"
    assert "arbitrary-sensitive-upstream-detail" not in caplog.text


def test_exchange_keeps_only_safe_reason(monkeypatch):
    from app.github_api import GitHubAPI

    gateway = GitHubAPI()
    monkeypatch.setattr(
        gateway,
        "_request",
        lambda *args, **kwargs: {
            "error": "incorrect_client_credentials",
            "error_description": "sensitive-upstream-detail",
        },
    )
    with pytest.raises(GitHubFailure) as caught:
        gateway.exchange(Settings(demo=True), "code", "verifier")
    assert caught.value.code == "incorrect_client_credentials"
    assert "sensitive-upstream-detail" not in str(caught.value)

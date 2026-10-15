"""Exercise the real GitHub adapter against HTTP fixtures, never real remote writes."""

import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.app import create_app
from app.code_changes import CONTENT, FILE_NAME
from app.config import Settings
from app.db import Base, CodeChange

ORIGIN = "http://localhost:8000"
NAME = "owner/real-repo"
BASE = "a" * 40
TREE = "b" * 40


class GitHubHTTP:
    def __init__(self):
        self.calls = []
        self.refs = {}
        self.commits = {BASE: {"sha": BASE, "tree": {"sha": TREE}, "parents": []}}
        self.trees = {
            TREE: [{"path": "README.md", "type": "blob", "mode": "100644", "sha": "c" * 40}]
        }
        self.prs = []
        self.head = BASE
        self.revoked = False
        self.writable = True
        self.repo_selected = True
        self.archived = False
        self.suspended = False
        self.empty = False
        self.user_id = 1
        self.fail_pr = False
        self.lose_pr_response = False
        self.lose_ref_response = False
        self.permissions = {"contents": "write", "pull_requests": "write"}

    @property
    def writes(self):
        return [
            (method, path, body)
            for method, path, body in self.calls
            if method == "POST" and path != "/login/oauth/access_token"
        ]

    def __call__(self, request):
        path = request.url.path
        method = request.method
        body = (
            json.loads(request.content)
            if request.headers.get("content-type") == "application/json"
            else {}
        )
        self.calls.append((method, path, body))
        status = 200
        if path == "/login/oauth/access_token":
            data = {"access_token": "test-user-token", "expires_in": 3600}
        else:
            assert request.url.host == "api.github.com"
            assert request.headers["Authorization"] == "Bearer test-user-token"
            if path == "/user":
                data = {"id": self.user_id, "login": f"user-{self.user_id}"}
            elif path == "/user/installations":
                data = {
                    "installations": []
                    if self.revoked
                    else [
                        {
                            "id": 9,
                            "account": {"login": "owner"},
                            "permissions": self.permissions,
                            "suspended_at": "2026-01-01" if self.suspended else None,
                        }
                    ]
                }
            elif path == "/user/installations/9/repositories":
                data = {
                    "repositories": [
                        {
                            "id": 123,
                            "full_name": NAME,
                            "private": True,
                            "default_branch": "main",
                            "permissions": {"push": self.writable},
                            "archived": self.archived,
                        }
                    ]
                    if self.repo_selected
                    else []
                }
            else:
                prefix = f"/repos/{NAME}"
                assert path.startswith(prefix), path
                route = path[len(prefix) :]
                if route == "/branches":
                    data = [] if self.empty else [{"name": "main", "commit": {"sha": self.head}}]
                elif route == "/branches/main":
                    data = {"name": "main", "commit": {"sha": self.head}}
                elif route.startswith("/git/commits/"):
                    data = self.commits[route.rsplit("/", 1)[1]]
                elif route.startswith("/git/trees/"):
                    data = {"tree": self.trees[route.rsplit("/", 1)[1]], "truncated": False}
                elif route == "/git/trees":
                    assert body["base_tree"] == TREE
                    assert body["tree"] == [
                        {"path": FILE_NAME, "mode": "100644", "type": "blob", "content": CONTENT}
                    ]
                    sha = hashlib.sha1(request.content).hexdigest()
                    self.trees[sha] = self.trees[TREE] + body["tree"]
                    data = {"sha": sha}
                elif route == "/git/commits":
                    assert body["parents"] == [BASE]
                    assert len(self.trees[body["tree"]]) == 2  # Existing README is preserved.
                    sha = hashlib.sha1(request.content).hexdigest()
                    self.commits[sha] = {
                        "sha": sha,
                        "tree": {"sha": body["tree"]},
                        "parents": [{"sha": BASE}],
                    }
                    data = {"sha": sha}
                elif route.startswith("/git/ref/heads/"):
                    branch = route.removeprefix("/git/ref/heads/")
                    if branch not in self.refs:
                        status, data = 404, {}
                    else:
                        data = {"object": {"sha": self.refs[branch]}}
                elif route == "/git/refs":
                    assert body["ref"].startswith("refs/heads/anyship/ai-placeholder-")
                    branch = body["ref"].removeprefix("refs/heads/")
                    assert branch not in self.refs
                    self.refs[branch] = body["sha"]
                    if self.lose_ref_response:
                        self.lose_ref_response = False
                        raise httpx.ReadTimeout("fixture timeout", request=request)
                    data = {"object": {"sha": body["sha"]}}
                elif route == "/pulls" and method == "GET":
                    branch = request.url.params["head"].split(":", 1)[1]
                    data = [pr for pr in self.prs if pr["head"]["ref"] == branch]
                elif route == "/pulls" and method == "POST":
                    assert body["draft"] is True and body["base"] == "main"
                    assert "AI" in body["body"] and "anyship-change:" in body["body"]
                    if self.fail_pr:
                        return httpx.Response(403, json={})
                    branch = body["head"]
                    number = len(self.prs) + 1
                    data = {
                        "number": number,
                        "html_url": f"https://github.com/{NAME}/pull/{number}",
                        "head": {"ref": branch, "sha": self.refs[branch], "repo": {"id": 123}},
                        "base": {"ref": "main", "repo": {"id": 123}},
                    }
                    self.prs.append(data)
                    if self.lose_pr_response:
                        self.lose_pr_response = False
                        raise httpx.ReadTimeout("fixture timeout", request=request)
                else:
                    raise AssertionError((method, route))
        return httpx.Response(status, json=data)


def login(client):
    start = client.get("/api/auth/github/start", follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    result = client.get(
        "/api/auth/github/callback",
        params={"state": state, "code": "fixture"},
        follow_redirects=False,
    )
    assert result.headers["location"] == "/"
    return {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/me").json()["csrf_token"]}


@pytest.fixture
def real(database_url, monkeypatch, request):
    remote = GitHubHTTP()
    with httpx.Client(transport=httpx.MockTransport(remote)) as transport:
        monkeypatch.setattr(httpx, "request", transport.request)
        settings = Settings(
            database_url=database_url,
            token_key=Fernet.generate_key().decode(),
            github_client_id="test",
            github_client_secret="test",
            github_app_slug="anyship",
            ai_mode=getattr(request, "param", "placeholder"),
        )
        app = create_app(settings)
        Base.metadata.create_all(app.state.engine)
        with TestClient(app, base_url=ORIGIN) as client:
            headers = login(client)
            project = client.post(
                "/api/projects",
                json={"repository_url": f"https://github.com/{NAME}", "branch": "main"},
                headers=headers,
            )
            assert project.status_code == 201
            yield app, client, remote, headers, f"/api/projects/{project.json()['id']}"


def prepare(real):
    _, client, _, headers, endpoint = real
    assert client.post(endpoint + "/analysis", json={}, headers=headers).status_code == 200
    response = client.post(endpoint + "/changes/apply", headers=headers)
    assert response.status_code == 200
    return response.json()


def publish(real, change):
    _, client, _, headers, endpoint = real
    return client.post(
        endpoint + "/changes/pr", headers=headers, json={"review_hash": change["review_hash"]}
    )


def test_full_flow_uses_actual_git_api_and_is_idempotent(real):
    _, client, remote, headers, endpoint = real
    resolved = client.post(
        "/api/github/resolve",
        headers=headers,
        json={"repository_url": f"https://github.com/{NAME}.git"},
    )
    assert resolved.json()["full_name"] == NAME
    change = prepare(real)
    assert not remote.writes
    assert CONTENT in change["content"] and f"+++ b/{FILE_NAME}" in change["diff"]
    response = publish(real, change)
    assert response.status_code == 200, response.text
    result = response.json()
    assert (
        result["status"] == "pr_created" and result["pr_url"] == f"https://github.com/{NAME}/pull/1"
    )
    assert remote.head == BASE and len(remote.refs) == 1
    assert client.get(endpoint + "/changes").json() == result
    count = len(remote.writes)
    assert publish(real, change).json() == result
    assert len(remote.writes) == count


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo",
        "https://evil.invalid/owner/repo",
        "https://github.com/owner/repo/tree/main",
        "https://token@github.com/owner/repo",
        "https://github.com/owner/repo?token=secret",
    ],
)
def test_url_validation_before_remote_requests(real, url):
    _, client, remote, headers, _ = real
    calls = len(remote.calls)
    response = client.post("/api/github/resolve", headers=headers, json={"repository_url": url})
    assert response.status_code == 422
    assert len(remote.calls) == calls


def test_revocation_write_permissions_and_csrf(real):
    _, client, remote, headers, endpoint = real
    change = prepare(real)
    assert (
        client.post(
            endpoint + "/changes/pr", json={"review_hash": change["review_hash"]}
        ).status_code
        == 403
    )
    remote.permissions["pull_requests"] = "read"
    assert publish(real, change).status_code == 403
    remote.permissions["pull_requests"] = "write"
    remote.writable = False
    assert publish(real, change).status_code == 403
    remote.writable = True
    remote.revoked = True
    assert publish(real, change).status_code == 404
    assert not remote.writes


def test_users_are_isolated(real):
    app, client, remote, _, endpoint = real
    prepare(real)
    remote.user_id = 2
    with TestClient(app, base_url=ORIGIN) as other:
        headers = login(other)
        assert other.get(endpoint + "/changes").status_code == 404
        for action in ("analysis", "changes/apply", "changes/pr"):
            response = other.post(
                endpoint + "/" + action, headers=headers, json={"review_hash": "a" * 64}
            )
            assert response.status_code == 404
    assert not remote.writes


def test_review_and_base_sha_must_match(real):
    app, client, remote, headers, endpoint = real
    change = prepare(real)
    assert publish(real, {**change, "review_hash": "0" * 64}).status_code == 409
    remote.head = "d" * 40
    assert publish(real, change).status_code == 409
    assert not remote.writes
    remote.head = BASE
    with app.state.sessions() as session:
        row = session.scalar(select(CodeChange))
        row.content += "# unreviewed\n"
        session.commit()
    assert publish(real, change).status_code == 409
    assert not remote.writes


def test_existing_file_is_not_overwritten(real):
    _, client, remote, headers, endpoint = real
    remote.trees[TREE].append({"path": FILE_NAME, "type": "blob"})
    assert client.post(endpoint + "/analysis", headers=headers, json={}).status_code == 409
    assert not remote.writes


def test_retry_after_pr_failure_reuses_commit_and_branch(real):
    _, _, remote, _, _ = real
    change = prepare(real)
    remote.fail_pr = True
    assert publish(real, change).status_code == 403
    commit_count = len(remote.commits)
    remote.fail_pr = False
    assert publish(real, change).status_code == 200
    assert len(remote.commits) == commit_count and len(remote.refs) == 1 and len(remote.prs) == 1


def test_ambiguous_responses_recover_without_duplicates(real):
    _, _, remote, _, _ = real
    change = prepare(real)
    remote.lose_ref_response = remote.lose_pr_response = True
    assert publish(real, change).status_code == 200
    assert len(remote.refs) == len(remote.prs) == 1


def test_busy_lease_and_restart_recovery(real):
    app, client, remote, headers, endpoint = real
    change = prepare(real)
    with app.state.sessions() as session:
        row = session.scalar(select(CodeChange))
        row.status, row.lease_until = "publishing", int(time.time()) + 600
        session.commit()
    assert publish(real, change).status_code == 409
    assert (
        client.post(endpoint + "/analysis", headers=headers, json={"restart": True}).status_code
        == 409
    )
    assert not remote.writes
    with app.state.sessions() as session:
        row = session.scalar(select(CodeChange))
        row.lease_until = int(time.time()) - 1
        session.commit()
    assert publish(real, change).status_code == 200


def test_remote_branch_is_never_overwritten(real):
    _, _, remote, _, _ = real
    change = prepare(real)
    remote.refs[change["branch"]] = "f" * 40
    assert publish(real, change).status_code == 409
    assert not remote.writes


def test_refresh_invalidates_review_and_uses_new_branch(real):
    _, client, remote, headers, endpoint = real
    change = prepare(real)
    response = client.post(endpoint + "/analysis", headers=headers, json={"restart": True})
    assert response.status_code == 200
    assert response.json()["branch"] != change["branch"]
    assert publish(real, change).status_code == 409
    assert not remote.writes


def test_production_disallows_ai_placeholder():
    with pytest.raises(ValueError, match="placeholder"):
        Settings(production=True, ai_mode="placeholder")


@pytest.mark.parametrize("real", ["unavailable"], indirect=True)
def test_unavailable_ai_does_not_fall_back_to_placeholder(real):
    _, client, remote, headers, endpoint = real
    for route in ("analysis", "changes/apply", "changes/pr"):
        result = client.post(
            endpoint + "/" + route, headers=headers, json={"review_hash": "a" * 64}
        )
        assert result.status_code == 503
    assert not remote.writes


def test_refresh_recovers_pr_after_server_checkpoint_was_lost(real):
    app, client, remote, headers, endpoint = real
    change = prepare(real)
    assert publish(real, change).status_code == 200
    # Simulate process death after GitHub success and before saving its response.
    with app.state.sessions() as session:
        row = session.scalar(select(CodeChange))
        row.status, row.pr_url, row.pr_number = "applied", "", 0
        session.commit()
    result = client.post(endpoint + "/analysis", headers=headers, json={"restart": True})
    assert result.json()["pr_url"] == f"https://github.com/{NAME}/pull/1"
    assert result.json()["status"] == "pr_created"
    assert len(remote.prs) == 1

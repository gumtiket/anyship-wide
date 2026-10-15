from unittest.mock import patch

from fastapi.testclient import TestClient

from app import demo_changes
from tests.test_web import ORIGIN, add, login, oauth_start


def prepare(client):
    headers = login(client)
    project = add(client, headers).json()
    endpoint = f"/api/projects/{project['id']}/demo-change"
    return headers, endpoint


def test_demo_creates_real_local_file_and_commit_without_network(web, tmp_path):
    _, client = web
    headers, endpoint = prepare(client)
    with patch("httpx.request", side_effect=AssertionError("Demo must not use network")):
        assert client.get(endpoint).json() is None
        proposed = client.post(endpoint, headers=headers).json()
        assert proposed["status"] == "proposed"
        assert not (tmp_path / "demo-workspaces").exists()
        assert client.post(endpoint, headers=headers).json()["id"] == proposed["id"]
        applied = client.post(endpoint + "/apply", headers=headers)
        assert applied.status_code == 200
        change = applied.json()
        assert change["status"] == "applied"
        assert "+# AI 분석 모듈이 아직 연결되지 않았습니다." in change["diff"]
        folder = tmp_path / "demo-workspaces" / change["id"]
        assert (folder / demo_changes.FILE_NAME).read_text(encoding="utf-8") == demo_changes.CONTENT
        assert demo_changes.git(folder, "remote", "-v") == ""
        assert client.post(endpoint + "/apply", headers=headers).json() == change
        result = client.post(
            endpoint + "/pr", headers=headers, json={"review_hash": change["review_hash"]}
        )
        assert result.status_code == 200
        result = result.json()
        assert result["status"] == "pr_preview"
        assert result["github_pr_url"] is None
        assert (
            demo_changes.git(folder, "show", f"HEAD:{demo_changes.FILE_NAME}")
            == demo_changes.CONTENT.strip()
        )
        assert demo_changes.git(folder, "status", "--porcelain") == ""
        assert client.get(endpoint).json() == result
        assert (
            client.post(
                endpoint + "/pr", headers=headers, json={"review_hash": change["review_hash"]}
            ).json()
            == result
        )
        assert demo_changes.git(folder, "rev-list", "--count", "HEAD") == "2"


def test_requires_selection_execution_and_exact_review_hash(web):
    _, client = web
    headers, endpoint = prepare(client)
    assert client.post(endpoint + "/apply", headers=headers).status_code == 409
    client.post(endpoint, headers=headers)
    assert (
        client.post(endpoint + "/pr", headers=headers, json={"review_hash": "0" * 64}).status_code
        == 409
    )
    client.post(endpoint + "/apply", headers=headers)
    assert (
        client.post(endpoint + "/pr", headers=headers, json={"review_hash": "0" * 64}).status_code
        == 409
    )
    assert client.get(endpoint).json()["status"] == "applied"


def test_demo_ownership_and_csrf_protect_every_action(web):
    app, first = web
    headers, endpoint = prepare(first)
    assert first.post(endpoint, headers={"Origin": ORIGIN}).status_code == 403
    first.post(endpoint, headers=headers)
    change = first.post(endpoint + "/apply", headers=headers).json()
    with TestClient(app, base_url=ORIGIN) as second:
        other_headers = login(second)
        assert second.get(endpoint).status_code == 404
        for suffix in ("", "/apply", "/pr"):
            assert (
                second.post(
                    endpoint + suffix,
                    headers=other_headers,
                    json={"review_hash": change["review_hash"]},
                ).status_code
                == 404
            )
    for suffix in ("/apply", "/pr"):
        assert (
            first.post(
                endpoint + suffix,
                headers={"Origin": ORIGIN},
                json={"review_hash": change["review_hash"]},
            ).status_code
            == 403
        )


def test_demo_endpoints_are_disabled_in_real_mode(real_web):
    _, client, _ = real_web
    state = oauth_start(client)
    client.get("/api/auth/github/callback", params={"state": state, "code": "code"})
    me = client.get("/api/me").json()
    headers = {"Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"]}
    project = add(client, headers).json()
    endpoint = f"/api/projects/{project['id']}/demo-change"
    assert client.get("/api/config").json()["demo_changes_available"] is False
    assert client.get(endpoint).status_code == 404
    for suffix in ("", "/apply", "/pr"):
        assert (
            client.post(
                endpoint + suffix, headers=headers, json={"review_hash": "0" * 64}
            ).status_code
            == 404
        )


def test_local_file_changed_after_review_is_not_committed(web, tmp_path):
    _, client = web
    headers, endpoint = prepare(client)
    client.post(endpoint, headers=headers)
    change = client.post(endpoint + "/apply", headers=headers).json()
    folder = tmp_path / "demo-workspaces" / change["id"]
    (folder / demo_changes.FILE_NAME).write_text("# changed after review\n", encoding="utf-8")
    assert (
        client.post(
            endpoint + "/pr", headers=headers, json={"review_hash": change["review_hash"]}
        ).status_code
        == 409
    )
    assert demo_changes.git(folder, "rev-list", "--count", "HEAD") == "1"


def test_unreviewed_staged_file_is_not_committed(web, tmp_path):
    _, client = web
    headers, endpoint = prepare(client)
    client.post(endpoint, headers=headers)
    change = client.post(endpoint + "/apply", headers=headers).json()
    folder = tmp_path / "demo-workspaces" / change["id"]
    (folder / "extra.txt").write_text("unreviewed", encoding="utf-8")
    demo_changes.git(folder, "add", "extra.txt")
    assert (
        client.post(
            endpoint + "/pr", headers=headers, json={"review_hash": change["review_hash"]}
        ).status_code
        == 409
    )
    assert demo_changes.git(folder, "rev-list", "--count", "HEAD") == "1"


def test_apply_failure_records_state_and_can_retry(web):
    _, client = web
    headers, endpoint = prepare(client)
    client.post(endpoint, headers=headers)
    with patch(
        "app.demo_changes.apply", side_effect=demo_changes.DemoFailure("internal diagnostic")
    ):
        response = client.post(endpoint + "/apply", headers=headers)
    assert response.status_code == 409
    assert "internal diagnostic" not in response.text
    assert client.get(endpoint).json()["status"] == "apply_failed"
    assert client.post(endpoint + "/apply", headers=headers).json()["status"] == "applied"

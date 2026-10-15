"""Real AI subprocess and GitHub HTTP boundary, without paid calls or remote writes."""

import base64
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import analyses
from app.ai_provider import ProcessAIProvider, worker_environment
from app.analysis_contract import ProposalFile, WorkerResult
from app.analysis_source import AnalysisError, Snapshot, blob_sha, fetch_snapshot
from app.app import create_app
from app.config import Settings
from app.db import AnalysisRun, AnalysisSlot, Base
from app.github_api import GitHubAPI

from .test_real_workflow import BASE, NAME, ORIGIN, TREE, GitHubHTTP, login


class AnalysisGitHub(GitHubHTTP):
    def __init__(self, files=None):
        super().__init__()
        files = files or {
            "app.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            "README.md": "# Original\n",
        }
        self.blobs = {blob_sha(value): value.encode() for value in files.values()}
        self.trees[TREE] = [
            {
                "path": name,
                "type": "blob",
                "mode": "100644",
                "sha": blob_sha(value),
                "size": len(value.encode()),
            }
            for name, value in files.items()
        ]

    def __call__(self, request):
        route = request.url.path.removeprefix(f"/repos/{NAME}")
        custom = (
            route.startswith("/git/blobs/")
            or request.method == "POST"
            and route in ("/git/trees", "/git/commits", "/git/refs", "/pulls")
        )
        if not custom:
            return super().__call__(request)
        body = json.loads(request.content) if request.content else {}
        assert request.headers["Authorization"] == "Bearer test-user-token"
        self.calls.append((request.method, request.url.path, body))
        if route.startswith("/git/blobs/"):
            raw = self.blobs[route.rsplit("/", 1)[1]]
            data = {"encoding": "base64", "content": base64.b64encode(raw).decode()}
        elif route == "/git/trees":
            assert body["base_tree"] == TREE
            sha = hashlib.sha1(request.content).hexdigest()
            entries = {item["path"]: item for item in self.trees[TREE]}
            entries.update({item["path"]: item for item in body["tree"]})
            self.trees[sha] = list(entries.values())
            data = {"sha": sha}
        elif route == "/git/commits":
            assert body["parents"] == [BASE]
            sha = hashlib.sha1(request.content).hexdigest()
            data = {"sha": sha, "tree": {"sha": body["tree"]}, "parents": [{"sha": BASE}]}
            self.commits[sha] = data
        elif route == "/git/refs":
            assert body["ref"].startswith("refs/heads/anyship/analysis-")
            branch = body["ref"].removeprefix("refs/heads/")
            assert branch not in self.refs
            self.refs[branch] = body["sha"]
            if self.lose_ref_response:
                self.lose_ref_response = False
                raise httpx.ReadTimeout("fixture", request=request)
            data = {"object": {"sha": body["sha"]}}
        else:
            assert body["draft"] and "anyship-analysis:" in body["body"]
            if self.fail_pr:
                return httpx.Response(403, json={})
            branch, number = body["head"], len(self.prs) + 1
            data = {
                "number": number,
                "html_url": f"https://github.com/{NAME}/pull/{number}",
                "head": {"ref": branch, "sha": self.refs[branch], "repo": {"id": 123}},
                "base": {"ref": body["base"], "repo": {"id": 123}},
            }
            self.prs.append(data)
            if self.lose_pr_response:
                self.lose_pr_response = False
                raise httpx.ReadTimeout("fixture", request=request)
        return httpx.Response(200, json=data)


class FixedProvider:
    def __init__(self):
        self.calls = 0
        self.release = threading.Event()
        self.release.set()
        self.error = None

    def run(self, work, request, log, cancelled):
        self.calls += 1
        while not self.release.wait(0.01):
            if cancelled():
                raise AnalysisError("interrupted", "중단")
        if self.error:
            raise self.error
        assert "token" not in json.dumps(request)
        log("분석 중")
        return WorkerResult(
            base_sha=request["base_sha"],
            report={
                "status": "partial",
                "transformation": {
                    "status": "proposed",
                    "needs_approval": True,
                    "addressed_ids": ["V1"],
                    "deferred_ids": ["V2"],
                },
                "gate_report": {"status": "skipped", "pr_eligible": False},
            },
            files=[
                ProposalFile(
                    path="app.py",
                    before_sha=blob_sha((work / "repo/app.py").read_bytes()),
                    content="from fastapi import FastAPI\napp = FastAPI()\nPORT = 8080\n",
                ),
                ProposalFile(path="Dockerfile", content="FROM python:3.12-slim\n"),
            ],
        )


@pytest.fixture
def connected(database_url, tmp_path, monkeypatch):
    remote, provider = AnalysisGitHub(), FixedProvider()
    with httpx.Client(transport=httpx.MockTransport(remote)) as transport:
        monkeypatch.setattr(httpx, "request", transport.request)
        settings = Settings(
            database_url=database_url,
            token_key=Fernet.generate_key().decode(),
            github_client_id="test",
            github_client_secret="test",
            github_app_slug="anyship",
            ai_mode="bronze",
            ai_provider="fake",
            ai_workspace=tmp_path / "ai",
        )
        app = create_app(settings, ai_provider=provider)
        Base.metadata.create_all(app.state.engine)
        with TestClient(app, base_url=ORIGIN) as client:
            headers = login(client)
            project = client.post(
                "/api/projects",
                headers=headers,
                json={"repository_url": f"https://github.com/{NAME}", "branch": "main"},
            )
            assert project.status_code == 201, project.text
            yield app, client, remote, provider, headers, "/api/projects/" + project.json()["id"]


def start(connected, *, wait=True, request_id=None):
    _, client, _, _, headers, root = connected
    response = client.post(
        root + "/analyses",
        headers=headers,
        json={"request_id": request_id or str(uuid.uuid4()), "target_env": "aws"},
    )
    assert response.status_code in (200, 202), response.text
    row = response.json()
    deadline = time.monotonic() + 10
    while wait and row["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, row
        time.sleep(0.02)
        row = client.get(root + "/analyses/" + row["id"]).json()
    return row


def publish(connected, row, **body):
    _, client, _, _, headers, root = connected
    return client.post(
        root + "/analyses/" + row["id"] + "/pr",
        headers=headers,
        json={"review_hash": row["review_hash"], "approve_risky": True, **body},
    )


def test_multi_file_review_and_ambiguous_publication_are_idempotent(connected):
    app, client, remote, provider, headers, root = connected
    identifier = str(uuid.uuid4())
    row = start(connected, request_id=identifier)
    assert row["status"] == "completed", row
    assert len(row["files"]) == 2 and "PORT = 8080" in row["diff"]
    assert not remote.writes
    assert start(connected, request_id=identifier)["id"] == row["id"] and provider.calls == 1
    assert publish(connected, row, approve_risky=False).status_code == 409
    assert publish(connected, row, review_hash="f" * 64).status_code == 409
    assert not remote.writes
    remote.lose_ref_response = remote.lose_pr_response = True
    result = publish(connected, row)
    assert result.status_code == 200, result.text
    assert result.json()["publish_status"] == "pr_created"
    assert remote.head == BASE and len(remote.prs) == len(remote.refs) == 1
    tree = remote.trees[remote.commits[result.json()["commit_sha"]]["tree"]["sha"]]
    assert {f["path"] for f in tree} == {"README.md", "app.py", "Dockerfile"}
    writes = len(remote.writes)
    assert publish(connected, row).status_code == 200 and len(remote.writes) == writes
    assert len(client.get(root + "/analyses").json()) == 1
    assert not list(app.state.analysis_runner.settings.ai_workspace.glob("job-*"))


def test_stale_sha_permissions_csrf_and_review_integrity(connected):
    app, client, remote, _, headers, root = connected
    row = start(connected)
    path = root + "/analyses/" + row["id"] + "/pr"
    assert client.post(path, json={"review_hash": row["review_hash"]}).status_code == 403
    remote.head = "d" * 40
    assert publish(connected, row).status_code == 409
    remote.head = BASE
    remote.revoked = True
    assert publish(connected, row).status_code == 404
    remote.revoked = False
    with app.state.sessions() as session:
        saved = session.get(AnalysisRun, row["id"])
        saved.files_json = "[]"
        session.commit()
    assert publish(connected, row).status_code == 409
    assert not remote.writes


def test_publish_retry_reuses_commit_after_github_failure(connected):
    _, _, remote, _, _, _ = connected
    row = start(connected)
    remote.fail_pr = True
    assert publish(connected, row).status_code == 403
    commits = len(remote.commits)
    remote.fail_pr = False
    assert publish(connected, row).status_code == 200
    assert len(remote.commits) == commits


def test_busy_project_blocks_duplicate_work_and_deletion(connected):
    app, client, _, provider, headers, root = connected
    provider.release.clear()
    try:
        identifier = str(uuid.uuid4())
        row = start(connected, wait=False, request_id=identifier)
        assert start(connected, wait=False, request_id=identifier)["id"] == row["id"]
        response = client.post(
            root + "/analyses", headers=headers, json={"request_id": str(uuid.uuid4())}
        )
        assert response.status_code == 409
        assert client.delete(root, headers=headers).status_code == 409
    finally:
        provider.release.set()
    assert start(connected, request_id=identifier)["status"] == "completed"


def test_failure_releases_slot_and_history_is_isolated(connected):
    app, client, remote, provider, headers, root = connected
    provider.error = AnalysisError("analysis_timeout", "분석 시간 초과")
    row = start(connected)
    assert row["status"] == "failed" and row["error_code"] == "analysis_timeout"
    provider.error = None
    assert start(connected)["status"] == "completed"
    remote.user_id = 2
    other_headers = login(client)
    assert client.get(root + "/analyses").status_code == 404
    assert client.get(root + "/analyses/" + row["id"]).status_code == 404
    assert (
        client.post(
            root + "/analyses", headers=other_headers, json={"request_id": str(uuid.uuid4())}
        ).status_code
        == 404
    )


def test_recovery_marks_abandoned_jobs_without_deleting_history(connected):
    app, _, _, _, _, root = connected
    row = start(connected)
    with app.state.sessions() as session:
        saved = session.get(AnalysisRun, row["id"])
        saved.status = "running"
        slot = session.get(AnalysisSlot, saved.project_id)
        slot.active_id, slot.lease_until = saved.id, int(time.time()) + 600
        session.commit()
        analyses.interrupt(session)
        assert saved.status == "interrupted" and slot.lease_until == 0
    assert start(connected)["status"] == "completed"


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "../escape"},
        {"path": "C:/escape"},
        {"path": "NUL.py"},
        {"path": "link", "type": "blob", "mode": "120000"},
        {"path": "submodule", "type": "commit", "mode": "160000"},
        {"path": "large.py", "type": "blob", "mode": "100644", "size": 524289, "sha": "a" * 40},
    ],
)
def test_source_rejects_unsafe_trees(tmp_path, monkeypatch, entry):
    remote = AnalysisGitHub()
    remote.trees[TREE] = [entry]
    with httpx.Client(transport=httpx.MockTransport(remote)) as transport:
        monkeypatch.setattr(httpx, "request", transport.request)
        with pytest.raises(AnalysisError):
            fetch_snapshot(GitHubAPI(), "test-user-token", NAME, BASE, tmp_path / "repo")
    assert not (tmp_path / "repo").exists()


def test_snapshot_preserves_crlf_and_excludes_sensitive_files(tmp_path, monkeypatch):
    remote = AnalysisGitHub({"app.py": "print(1)\r\n", ".env": "TOKEN=secret", "data.db": "data"})
    with httpx.Client(transport=httpx.MockTransport(remote)) as transport:
        monkeypatch.setattr(httpx, "request", transport.request)
        snapshot = fetch_snapshot(GitHubAPI(), "test-user-token", NAME, BASE, tmp_path / "repo")
    assert snapshot.files == {"app.py": "print(1)\r\n"} and snapshot.skipped == 2
    assert (tmp_path / "repo/app.py").read_bytes() == b"print(1)\r\n"
    assert ".env" in snapshot.entries
    assert len([c for c in remote.calls if "/git/blobs/" in c[1]]) == 1


def test_bundle_rejects_mismatched_original_and_python_errors():
    snapshot = Snapshot(
        BASE,
        TREE,
        {"app.py": "print(1)\n"},
        {"app.py": {"sha": blob_sha("print(1)\n"), "mode": "100755"}},
    )
    for content, before in [("print(2)", None), ("def bad(", blob_sha("print(1)\n"))]:
        with pytest.raises(AnalysisError):
            analyses.validated_bundle(
                snapshot,
                WorkerResult(
                    base_sha=BASE,
                    report={},
                    files=[ProposalFile(path="app.py", content=content, before_sha=before)],
                ),
            )
    result = WorkerResult(
        base_sha=BASE,
        report={},
        files=[ProposalFile(path="app.py", content="print(2)", before_sha=blob_sha("print(1)\n"))],
    )
    files, diff = analyses.validated_bundle(snapshot, result)
    assert files[0]["mode"] == "100755" and "\\ No newline at end of file" in diff


def test_worker_environment_excludes_service_and_ambient_credentials(tmp_path, monkeypatch):
    for key in (
        "APP_TOKEN_KEY",
        "APP_DATABASE_URL",
        "GITHUB_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, "must-not-leak")
    monkeypatch.setenv("APP_AI_AWS_ACCESS_KEY_ID", "ai-only")
    env = worker_environment(tmp_path)
    assert "must-not-leak" not in json.dumps(env)
    assert env["AWS_ACCESS_KEY_ID"] == "ai-only"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"


@pytest.mark.parametrize("sample", ["todo", "todo-scheduler", "memo-app"])
def test_real_ai_process_from_offline_sample(tmp_path, sample):
    root = Path(__file__).resolve().parents[2] / "AI/samples" / sample
    repo = tmp_path / "repo"
    repo.mkdir()
    for source in root.rglob("*"):
        if source.is_file():
            target = repo / source.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    provider = ProcessAIProvider(SimpleNamespace(ai_timeout=60))
    stages = []
    result = provider.run(
        tmp_path,
        {
            "provider": "fake",
            "base_sha": BASE,
            "source_repo": f"https://github.com/{NAME}",
            "app_name": "fixture-app",
            "target_env": "aws",
            "max_calls": 12,
        },
        stages.append,
        lambda: False,
    )
    assert result.base_sha == BASE and result.report["cost"]["external_calls"] == 0
    assert "분석 중" in stages
    assert result.report["gate_report"]["pr_eligible"] is False
    if sample == "memo-app":
        assert result.report["status"] == "unsupported"
    else:
        assert result.report["adapter_compatible"]
        assert {"Dockerfile", "deploy-spec.yaml", ".dockerignore"} <= {f.path for f in result.files}
        assert result.report["transformation"]["needs_approval"]


@pytest.mark.parametrize("provider", ["none", "fake"])
def test_reanalysis_accepts_deploy_spec_secret_flags(tmp_path, provider):
    repo = tmp_path / "repo"
    repo.mkdir()
    original = {
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "requirements.txt": "fastapi==0.115.12\nuvicorn==0.34.2\n",
        "deploy-spec.yaml": "env:\n  - name: LOG_LEVEL\n    secret: false\n"
        "  - name: APP_SECRET\n    secret: true\n",
    }
    for name, content in original.items():
        (repo / name).write_bytes(content.encode("utf-8"))
    result = ProcessAIProvider(SimpleNamespace(ai_timeout=60)).run(
        tmp_path,
        {
            "provider": provider,
            "base_sha": BASE,
            "source_repo": f"https://github.com/{NAME}",
            "app_name": "fixture-app",
            "target_env": "aws",
            "max_calls": 12,
        },
        lambda _: None,
        lambda: False,
    )
    assert result.report["status"] != "failed"
    assert result.report["adapter_compatible"]
    assert result.report["cost"]["external_calls"] == 0
    assert "deploy-spec.yaml" in {file.path for file in result.files}
    diff = analyses.make_diff(original, {file.path: file.content for file in result.files})
    assert "secret: false" in diff and "secret: true" in diff
    assert {name: (repo / name).read_bytes() for name in original} == {
        name: content.encode("utf-8") for name, content in original.items()
    }


def test_process_cancellation_terminates_child(tmp_path):
    with pytest.raises(AnalysisError, match="중단"):
        ProcessAIProvider(SimpleNamespace(ai_timeout=60)).run(
            tmp_path, {}, lambda _: None, lambda: True
        )
    # On Windows this deletion would fail while the output stream was still open.
    (tmp_path / "events.jsonl").unlink()


@pytest.mark.parametrize("provider", ["none", "fake"])
@pytest.mark.parametrize("binding", ["postgres", "unknown", "invalid"])
def test_reanalysis_keeps_database_contract_or_blocks_pr_files(tmp_path, provider, binding):
    import yaml

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text(
        "import os\nfrom fastapi import FastAPI\napp = FastAPI()\n"
        'DATABASE_URL = os.environ["DATABASE_URL"]\n'
    )
    (repo / "requirements.txt").write_text("fastapi==0.115.12\nuvicorn==0.34.2\n")
    original = (
        "backing_services:\n- type: postgres\n  bind_as: DATABASE_URL\n"
        "release:\n  migrate: alembic upgrade head\n"
    )
    if binding == "invalid":
        original = "backing_services: [\n"
    if binding != "unknown":
        (repo / "deploy-spec.yaml").write_text(original)
    request = {
        "provider": provider,
        "base_sha": BASE,
        "source_repo": f"https://github.com/{NAME}",
        "app_name": "fixture-app",
        "target_env": "onprem",
        "max_calls": 12,
    }
    result = ProcessAIProvider(SimpleNamespace(ai_timeout=60)).run(
        tmp_path, request, lambda _: None, lambda: False
    )
    if binding == "postgres":
        assert result.report["adapter_compatible"]
        spec = yaml.safe_load(next(f.content for f in result.files if f.path == "deploy-spec.yaml"))
        assert spec["backing_services"] == [{"type": "postgres", "bind_as": "DATABASE_URL"}]
        assert spec["release"]["migrate"] == "alembic upgrade head"
        assert (repo / "deploy-spec.yaml").read_text() == original
    else:
        assert result.report["status"] == "failed"
        assert not result.report["adapter_compatible"]
        assert result.files == []
        assert result.report["packaging_warnings"]


def test_real_pipeline_through_snapshot_review_and_git_api(connected):
    app, _, remote, _, _, _ = connected
    sample = Path(__file__).resolve().parents[2] / "AI/samples/todo"
    source = AnalysisGitHub(
        {
            p.relative_to(sample).as_posix(): p.read_bytes().decode().replace("\r\n", "\n")
            for p in sample.rglob("*")
            if p.is_file()
        }
    )
    remote.trees, remote.blobs = source.trees, source.blobs
    app.state.analysis_runner.provider = ProcessAIProvider(app.state.analysis_runner.settings)
    row = start(connected)
    assert row["status"] == "completed", row
    assert row["report"]["adapter_compatible"]
    assert len(row["files"]) >= 7
    result = publish(connected, row)
    assert result.status_code == 200, result.text
    assert len(remote.prs) == 1


def test_request_config_and_unknown_uuid_rejected(connected):
    _, client, _, _, headers, root = connected
    assert (
        client.post(root + "/analyses", headers=headers, json={"request_id": "-" * 36}).status_code
        == 422
    )
    identifier = str(uuid.uuid4())
    start(connected, request_id=identifier)
    assert (
        client.post(
            root + "/analyses",
            headers=headers,
            json={"request_id": identifier, "target_env": "onprem"},
        ).status_code
        == 409
    )
    assert client.get(root + "/analyses/" + str(uuid.uuid4())).status_code == 404


def test_new_files_cannot_replace_each_others_directories():
    result = WorkerResult(
        base_sha=BASE,
        report={},
        files=[
            ProposalFile(path="folder", content="text"),
            ProposalFile(path="folder/app.py", content="pass"),
        ],
    )
    with pytest.raises(AnalysisError, match="충돌"):
        analyses.validated_bundle(Snapshot(BASE, TREE, {}, {}), result)

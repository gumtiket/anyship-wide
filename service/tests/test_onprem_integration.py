import ast
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Deployment, OnpremEnvironment, OnpremRegistrationToken
from app.onprem_runner import OnpremRunner
from tests.onprem_fakes import RecordingTransport, load_fixture, parts
from tests.test_aws_onboarding import authenticate
from tests.test_deploy_api import build_web
from tests.test_deploy_runner import DeferredExecutor
from tests.test_web import ORIGIN, add

ENDPOINT = "/api/onprem/environments"


@pytest.fixture
def web(database_url, tmp_path):
    executor = DeferredExecutor()
    app, runner, sessions = build_web(database_url, tmp_path, executor=executor)
    fake = parts()
    transport = RecordingTransport(tmp_path)
    runner.onprem = OnpremRunner(runner, fake.deployer, transport)
    original = runner.source.fetch

    def fetch(*args):
        result = original(*args)
        return result.__class__(
            path=result.path,
            commit_sha=result.commit_sha,
            spec=load_fixture("specs").GENERATED,
            cleanup=result.cleanup,
        )

    runner.source.fetch = fetch
    with TestClient(app, base_url=ORIGIN) as client:
        headers = authenticate(client)
        project = add(client, headers).json()
        yield SimpleNamespace(
            client=client,
            headers=headers,
            app=app,
            sessions=sessions,
            runner=runner,
            executor=executor,
            fake=fake,
            transport=transport,
            project=project,
            endpoint=f"/api/projects/{project['id']}/deployment",
        )


def register(web, **extra):
    response = web.client.post(
        ENDPOINT,
        headers=web.headers,
        json={
            "request_id": str(uuid.uuid4()),
            "name": "내 서버",
            "email": "ops@example.com",
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def report(web, identifier, token=None, ip="3.38.88.141"):
    return web.client.post(
        f"{ENDPOINT}/{identifier}/ready",
        json={"token": token or web.transport.tokens[identifier], "public_ip": ip},
    )


def environment(web, identifier):
    return next(row for row in web.client.get(ENDPOINT).json() if row["id"] == identifier)


def verified(web):
    identifier = register(web)["environment"]["id"]
    assert report(web, identifier).status_code == 202
    assert environment(web, identifier)["status"] == "SIGNALED"
    web.executor.run_all()
    assert environment(web, identifier)["status"] == "VERIFIED"
    return identifier


def environment_job(web, identifier, action, request_id=None):
    return web.client.post(
        f"{ENDPOINT}/{identifier}/jobs",
        headers=web.headers,
        json={"request_id": request_id or str(uuid.uuid4()), "action": action},
    )


def test_registration_token_is_hashed_scoped_and_revealed_once(web):
    request_id = str(uuid.uuid4())
    created = register(web, request_id=request_id)
    identifier = created["environment"]["id"]
    token = web.transport.tokens[identifier]
    assert token in created["registration"]["command"]
    assert "ssh_user" not in created["environment"] and "ssh_port" not in created["environment"]
    with web.sessions() as session:
        row = session.get(OnpremEnvironment, identifier)
        assert row.host is None and row.credential_ref is None
        values = [
            str(getattr(item, c.name))
            for model in (OnpremEnvironment, OnpremRegistrationToken)
            for item in session.scalars(select(model))
            for c in model.__table__.columns
        ]
        assert token not in json.dumps(values)
    retry = web.client.post(
        ENDPOINT,
        headers=web.headers,
        json={"request_id": request_id, "name": "내 서버", "email": "ops@example.com"},
    )
    assert retry.status_code == 200 and retry.json()["registration"] is None
    other = register(web)["environment"]["id"]
    assert report(web, other, token=token).status_code == 401
    assert report(web, identifier).status_code == 202
    assert report(web, identifier).status_code == 401


def test_setup_token_stays_out_of_urls_and_validation_errors(web):
    created = register(web)
    identifier = created["environment"]["id"]
    token = web.transport.tokens[identifier]
    response = web.client.post(f"{ENDPOINT}/{identifier}/setup", json={"token": token})
    assert response.status_code == 200 and "docker compose up" in response.text
    assert token in response.text and response.headers["cache-control"] == "no-store"
    assert (
        f"/{token}" not in response.text and f"/{token}" not in created["registration"]["command"]
    )
    malformed = web.client.post(
        f"{ENDPOINT}/{identifier}/ready", json={"token": token, "public_ip": None}
    )
    assert malformed.status_code == 422 and token not in malformed.text


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "example.com", "8.8.8.8;id"]
)
def test_callback_rejects_non_public_addresses_without_consuming_token(web, address):
    identifier = register(web)["environment"]["id"]
    assert report(web, identifier, ip=address).status_code == 422
    assert environment(web, identifier)["status"] == "ISSUED"
    assert report(web, identifier).status_code == 202


def test_expired_and_reissued_tokens_are_rejected(web):
    identifier = register(web)["environment"]["id"]
    original = web.transport.tokens[identifier]
    with web.sessions() as session:
        session.scalar(select(OnpremRegistrationToken)).expires_at = int(time.time()) - 1
        session.commit()
    assert report(web, identifier).status_code == 401
    response = web.client.post(
        f"{ENDPOINT}/{identifier}/registration", headers=web.headers, json={}
    )
    assert response.status_code == 200
    assert report(web, identifier, token=original).status_code == 401
    assert report(web, identifier).status_code == 202


def test_failed_check_never_verifies_callback_and_can_retry(web):
    web.fake.server.responses[("true",)] = (255, b"", b"unreachable")
    identifier = register(web)["environment"]["id"]
    report(web, identifier)
    web.executor.run_all()
    row = environment(web, identifier)
    assert row["status"] == "SIGNALED" and row["last_seen_at"] is None
    assert row["error_code"] == "ssh_unreachable" and "22" in row["error_message"]
    del web.fake.server.responses[("true",)]
    assert environment_job(web, identifier, "check").status_code == 202
    web.executor.run_all()
    assert environment(web, identifier)["status"] == "VERIFIED"


def test_reported_address_must_match_independent_check(web):
    identifier = register(web)["environment"]["id"]
    report(web, identifier, ip="8.8.8.8")
    web.executor.run_all()
    row = environment(web, identifier)
    assert row["status"] == "SIGNALED" and row["public_ip"] is None
    assert row["error_code"] == "public_ip_mismatch"


def test_whole_lifecycle_uses_connect_factory_and_preserves_other_dns(web):
    identifier = verified(web)
    response = web.client.put(
        web.endpoint, headers=web.headers, json={"environment_id": identifier, "set_name": "onprem"}
    )
    assert response.status_code == 200, response.text
    assert (
        web.client.post(
            f"{ENDPOINT}/{identifier}/registration", headers=web.headers, json={}
        ).status_code
        == 409
    )
    web.fake.dns.records["other"] = "8.8.8.8"

    def run(action, **extra):
        accepted = web.client.post(
            web.endpoint + "/jobs",
            headers=web.headers,
            json={"request_id": str(uuid.uuid4()), "action": action, **extra},
        )
        assert accepted.status_code == 202, accepted.text
        assert environment_job(web, identifier, "remove_environment").status_code == 409
        web.executor.run_all()
        job = web.client.get(web.endpoint + "/jobs/" + accepted.json()["id"]).json()
        assert job["status"] == "succeeded", job
        return job

    run("deploy")
    assert len(web.fake.dns.records) == 2
    assert environment_job(web, identifier, "remove_environment").status_code == 409
    assert run("status")["result"]["state"] == "running"
    run("rollback", image_tag="abc1234def56")
    run("destroy")
    assert len(web.fake.dns.records) == 2
    assert environment_job(web, identifier, "remove_environment").status_code == 202
    web.executor.run_all()
    assert web.client.get(ENDPOINT).json() == []
    assert web.fake.dns.records == {"other": "8.8.8.8"}
    assert web.client.get(web.endpoint).json()["target"] is None
    assert web.runner.access.calls == 0


def test_environment_jobs_are_idempotent_and_exclusive(web):
    identifier = verified(web)
    request_id = str(uuid.uuid4())
    first = environment_job(web, identifier, "check", request_id)
    second = environment_job(web, identifier, "check", request_id)
    assert first.status_code == 202 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert environment_job(web, identifier, "remove_environment", request_id).status_code == 409
    assert environment_job(web, identifier, "check").status_code == 409


def test_removed_environment_retains_history_and_idempotent_result(web):
    identifier = register(web)["environment"]["id"]
    request_id = str(uuid.uuid4())
    first = environment_job(web, identifier, "remove_environment", request_id)
    assert first.status_code == 202
    web.executor.run_all()
    repeated = environment_job(web, identifier, "remove_environment", request_id)
    assert repeated.status_code == 200 and repeated.json()["id"] == first.json()["id"]
    assert repeated.json()["status"] == "succeeded"
    assert web.client.get(f"{ENDPOINT}/{identifier}/jobs").json()[0]["status"] == "succeeded"
    assert environment_job(web, identifier, "check").status_code == 409
    assert report(web, identifier).status_code == 401


def test_simultaneous_callbacks_consume_token_once(web):
    identifier = register(web)["environment"]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: report(web, identifier), range(2)))
    assert sorted(response.status_code for response in responses) == [202, 401]
    assert len(web.client.get(f"{ENDPOINT}/{identifier}/jobs").json()) == 1


def test_reissue_requires_a_new_signal_before_check(web):
    identifier = verified(web)
    assert (
        web.client.post(
            f"{ENDPOINT}/{identifier}/registration", headers=web.headers, json={}
        ).status_code
        == 200
    )
    assert environment_job(web, identifier, "check").status_code == 422
    assert environment(web, identifier)["active_job_id"] is None
    assert report(web, identifier).status_code == 202


def test_expired_job_cannot_overwrite_new_job_or_environment(web):
    identifier = verified(web)
    first = environment_job(web, identifier, "check").json()
    with web.sessions() as session:
        session.get(OnpremEnvironment, identifier).lease_until = 1
        session.commit()
    second = environment_job(web, identifier, "check")
    assert second.status_code == 202
    web.runner.onprem.finish_environment(first["id"], {"ok": False, "error": {"code": "late"}})
    assert environment(web, identifier)["status"] == "VERIFIED"
    assert environment(web, identifier)["active_job_id"] == second.json()["id"]
    web.executor.run_all()
    statuses = {
        row["id"]: row["status"] for row in web.client.get(f"{ENDPOINT}/{identifier}/jobs").json()
    }
    assert statuses[first["id"]] == "interrupted" and statuses[second.json()["id"]] == "succeeded"


def test_failed_deploy_blocks_environment_and_project_removal_until_destroy(web):
    identifier = verified(web)
    assert (
        web.client.put(
            web.endpoint,
            headers=web.headers,
            json={"environment_id": identifier, "set_name": "onprem"},
        ).status_code
        == 200
    )
    from anyship_adapters import AdapterError, DeployResult

    secret = "super-private-fixture-value"
    web.runner.onprem.deployer.deploy = lambda *args, **kwargs: DeployResult(
        ok=False, error=AdapterError(code="build_failed", message="failure " + secret)
    )
    accepted = web.client.post(
        web.endpoint + "/jobs",
        headers=web.headers,
        json={"request_id": str(uuid.uuid4()), "secrets": {"APP_TOKEN": secret}},
    )
    assert accepted.status_code == 202
    web.executor.run_all()
    job = web.client.get(web.endpoint + "/jobs/" + accepted.json()["id"])
    assert job.json()["status"] == "failed" and secret not in job.text
    assert environment_job(web, identifier, "remove_environment").status_code == 409
    assert (
        web.client.delete(f"/api/projects/{web.project['id']}", headers=web.headers).status_code
        == 409
    )
    cleanup = web.client.post(
        web.endpoint + "/jobs",
        headers=web.headers,
        json={"request_id": str(uuid.uuid4()), "action": "destroy"},
    )
    assert cleanup.status_code == 202, cleanup.text
    web.executor.run_all()
    assert (
        web.client.get(web.endpoint + "/jobs/" + cleanup.json()["id"]).json()["status"]
        == "succeeded"
    )
    assert environment_job(web, identifier, "remove_environment").status_code == 202


def test_owner_and_csrf_and_unknown_transport_are_enforced(web):
    identifier = register(web)["environment"]["id"]
    assert web.client.post(f"{ENDPOINT}/{identifier}/registration", json={}).status_code == 403
    bad = web.client.post(
        ENDPOINT,
        headers=web.headers,
        json={
            "request_id": str(uuid.uuid4()),
            "name": "n",
            "email": "ops@example.com",
            "connection_kind": "agent",
        },
    )
    assert bad.status_code == 422
    with web.sessions() as session:
        # DB 외래 키를 만족하도록 이미 있는 다른 소유자를 쓴다.
        from app.db import User

        session.add(User(id="someone-else", github_id=9988, login="other", name="other"))
        session.flush()
        session.get(OnpremEnvironment, identifier).created_by = "someone-else"
        session.commit()
    assert web.client.get(ENDPOINT).json() == []
    assert environment_job(web, identifier, "check").status_code == 404


def test_dns_cleanup_failure_keeps_registration_for_retry(web):
    from anyship_adapters import AdapterError
    from anyship_adapters.dns import DnsError

    identifier = verified(web)
    original = web.fake.dns.remove

    def fail(*args):
        raise DnsError(AdapterError(code="dns_failed", message="retry cleanup"))

    web.fake.dns.remove = fail
    assert environment_job(web, identifier, "remove_environment").status_code == 202
    web.executor.run_all()
    assert environment(web, identifier)["error_code"]
    assert web.client.get(f"{ENDPOINT}/{identifier}/jobs").json()[0]["status"] == "failed"
    web.fake.dns.remove = original
    assert environment_job(web, identifier, "remove_environment").status_code == 202
    web.executor.run_all()
    assert web.client.get(ENDPOINT).json() == []


def test_same_app_name_cannot_overwrite_another_project(web):
    identifier = verified(web)
    assert (
        web.client.put(
            web.endpoint,
            headers=web.headers,
            json={"environment_id": identifier, "set_name": "onprem"},
        ).status_code
        == 200
    )
    from app.db import Project

    with web.sessions() as session:
        original = session.get(Project, web.project["id"])
        session.add(
            Project(
                id="p-other",
                workspace_id=original.workspace_id,
                created_by=original.created_by,
                repository_id=999998,
                installation_id=1,
                full_name="o/other",
                branch="main",
                base_sha="a" * 40,
                created_at=1,
            )
        )
        session.flush()
        session.add(
            Deployment(
                project_id="p-other",
                onprem_environment_id=identifier,
                set_name="onprem",
                app_name="todo",
            )
        )
        session.commit()
    accepted = web.client.post(
        web.endpoint + "/jobs", headers=web.headers, json={"request_id": str(uuid.uuid4())}
    )
    assert accepted.status_code == 409 and accepted.json()["detail"]["code"] == "app_name_in_use"
    assert environment(web, identifier)["active_job_id"] is None
    with web.sessions() as session:
        assert session.get(Deployment, web.project["id"]).app_name == ""


def forbidden_transport_usage(source):
    tree = ast.parse(source)
    return any(
        (isinstance(node, ast.Name) and node.id in {"SshRunner", "SshConnection"})
        or (isinstance(node, ast.Attribute) and node.attr in {"SshRunner", "SshConnection"})
        or (isinstance(node, ast.ImportFrom) and node.module == "anyship_adapters.ssh")
        or (
            isinstance(node, ast.Import)
            and any(alias.name == "anyship_adapters.ssh" for alias in node.names)
        )
        for node in ast.walk(tree)
    )


def test_transport_boundary_and_mutation_detection():
    sources = Path(__file__).resolve().parents[1] / "app"
    assert [
        p.name
        for p in sources.glob("*.py")
        if forbidden_transport_usage(p.read_text(encoding="utf-8"))
    ] == ["onprem_transport.py"]
    api = (sources / "onprem_api.py").read_text(encoding="utf-8")
    assert forbidden_transport_usage(
        api + "\nfrom anyship_adapters.ssh import SshRunner as Remote\n"
    )
    assert forbidden_transport_usage(api + "\nimport anyship_adapters.ssh as remote\n")

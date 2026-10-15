"""환경 이전(`migrate`)과 이전 앱 삭제(`destroy_previous`). 로그인, 프로젝트, 환경, 선점, 저장은 진짜 코드를 쓰고,
배포자와 데이터 이전만 가짜로 끼운다(데이터 이전의 명령 자체는 `infra/adapters/tests/test_data_transfer.py`가 본다)."""

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from anyship_adapters import AdapterError, DeployResult, DestroyResult, LogEvent, TransferResult
from app.db import AwsEnvironment, DeployJob, Deployment, OnpremEnvironment
from app.migration_runner import MigrationRunner
from app.onprem_runner import OnpremRunner
from tests.onprem_fakes import RecordingTransport, load_fixture, parts
from tests.test_aws_onboarding import authenticate
from tests.test_deploy_api import build_web, make_environment, put_target
from tests.test_deploy_runner import FOUNDATION, USER_SECRET, DeferredExecutor
from tests.test_onprem_integration import ENDPOINT, environment_job, verified
from tests.test_web import ORIGIN, add


class FakeTransferDeployer:
    """데이터 이전용 배포자. 받은 호출을 기록하고, 결과를 바꿔 끼울 수 있다."""

    def __init__(self):
        self.calls, self.size = [], 2048
        self.check_result = self.transfer_result = self.destroy_result = None
        self.raise_in = None

    def _maybe_raise(self, where):
        if self.raise_in == where:
            raise RuntimeError(USER_SECRET)

    def check_transfer_source(self, env, app, log, *, set_name, max_bytes=0):
        self.calls.append(("check", env, app, set_name))
        self._maybe_raise("check")
        log(LogEvent(step=1, total=1, name="원본 확인", message="원본을 확인했습니다"))
        return self.check_result or TransferResult(ok=True, details={"bytes": self.size})

    def transfer_data(
        self, source_env, target_env, app, log, *, source_set, target_set, max_bytes=0
    ):
        self.calls.append(("transfer", source_env, target_env, app, source_set, target_set))
        self._maybe_raise("transfer")
        log(LogEvent(step=3, total=5, name="데이터 복사", message="옮기는 중"))
        return self.transfer_result or TransferResult(
            ok=True, details={"bytes": self.size, "tables": 2, "rows": 15, "source_stopped": True}
        )

    def destroy(self, env, app, log, *, set_name):
        self.calls.append(("destroy", env, app, set_name))
        self._maybe_raise("destroy")
        log(LogEvent(step=1, total=1, name="앱 삭제", message="지우는 중"))
        return self.destroy_result or DestroyResult(ok=True)


@pytest.fixture
def web(database_url, tmp_path):
    executor = DeferredExecutor()
    app, runner, sessions = build_web(database_url, tmp_path, executor=executor)
    fake, transfer = parts(), FakeTransferDeployer()
    transport = RecordingTransport(tmp_path)
    runner.onprem = OnpremRunner(runner, fake.deployer, transport)
    runner.migration = MigrationRunner(runner, transfer)
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
            transfer=transfer,
            transport=transport,
            project=project,
            endpoint=f"/api/projects/{project['id']}/deployment",
        )


def job_request(web, body):
    return web.client.post(
        web.endpoint + "/jobs", headers=web.headers, json={"request_id": str(uuid.uuid4()), **body}
    )


def finish_job(web, accepted):
    assert accepted.status_code == 202, accepted.text
    web.executor.run_all()
    return web.client.get(web.endpoint + "/jobs/" + accepted.json()["id"]).json()


def deployed_on_aws(web):
    """프로젝트가 AWS 환경에 배포된 상태를 만든다. AWS 환경 ID를 돌려준다."""
    identifier = make_environment(web)
    assert put_target(web, identifier).status_code == 200
    job = finish_job(web, job_request(web, {"secrets": {}}))
    assert job["status"] == "succeeded", job
    return identifier


def deployed_on_onprem(web):
    identifier = verified(web)
    assert put_target(web, identifier, "onprem").status_code == 200
    job = finish_job(web, job_request(web, {"secrets": {}}))
    assert job["status"] == "succeeded", job
    return identifier


def migrate(web, destination, set_name, **extra):
    return job_request(
        web,
        {
            "action": "migrate",
            "target_environment_id": destination,
            "target_set_name": set_name,
            **extra,
        },
    )


def target_row(web):
    with web.sessions() as session:
        row = session.get(Deployment, web.project["id"])
        session.expunge(row)
        return row


def everything_in_the_database(web):
    with web.sessions() as session:
        return json.dumps(
            [
                [str(getattr(row, c.name)) for c in row.__table__.columns]
                for model in (DeployJob, Deployment)
                for row in session.scalars(select(model))
            ],
            ensure_ascii=False,
        )


# --- 성공: AWS에서 온프레미스로, 온프레미스에서 AWS로 ---------------------------------------------------
def test_an_aws_app_moves_to_an_onprem_environment_and_the_project_follows_it(web):
    aws = deployed_on_aws(web)
    old = target_row(web)
    destination = verified(web)
    job = finish_job(web, migrate(web, destination, "onprem"))
    assert job["status"] == "succeeded" and job["action"] == "migrate", job
    new = target_row(web)
    assert (new.aws_environment_id, new.onprem_environment_id, new.set_name) == (
        None,
        destination,
        "onprem",
    )
    assert new.app_name == "todo" and new.url == job["result"]["url"] and new.url != old.url
    assert (
        new.previous_kind,
        new.previous_environment_id,
        new.previous_app_name,
        new.previous_url,
    ) == ("aws", aws, "todo", old.url)
    kinds = [call[0] for call in web.transfer.calls]
    assert kinds == ["check", "transfer"]  # 대상 배포 전에 원본부터 확인한다
    assert (
        web.transfer.calls[1][4:] == ("aws-always-on", "onprem")
        and job["result"]["details"]["rows"] == 15
    )


def test_an_onprem_app_moves_to_an_aws_environment(web):
    onprem = deployed_on_onprem(web)
    aws = make_environment(web, name="새 AWS")
    job = finish_job(web, migrate(web, aws, "aws-always-on"))
    assert job["status"] == "succeeded", job
    new = target_row(web)
    assert (new.aws_environment_id, new.onprem_environment_id, new.set_name) == (
        aws,
        None,
        "aws-always-on",
    )
    assert (new.previous_kind, new.previous_environment_id) == ("onprem", onprem)
    assert web.transfer.calls[1][4:] == ("onprem", "aws-always-on")


def test_the_overview_shows_the_stopped_app_that_was_left_behind(web):
    deployed_on_aws(web)
    finish_job(web, migrate(web, verified(web), "onprem"))
    target = web.client.get(web.endpoint).json()["target"]
    assert target["set_name"] == "onprem" and target["previous"]["kind"] == "aws"
    assert (
        target["previous"]["app_name"] == "todo"
        and target["previous"]["environment_name"] == "테스트 계정"
    )
    assert target["previous"]["url"].startswith("https://todo.")


def test_the_target_environment_is_deployed_with_the_latest_source_and_the_given_secrets(web):
    deployed_on_aws(web)
    destination = make_environment(
        web, name="두 번째 AWS", role_arn="arn:aws:iam::223455088214:role/other"
    )
    finish_job(web, migrate(web, destination, "aws-always-on", secrets={"APP_TOKEN": USER_SECRET}))
    call = web.runner.deployer.calls[-1]
    assert call["secrets"] == {"APP_TOKEN": USER_SECRET} and call["set_name"] == "aws-always-on"
    assert USER_SECRET not in everything_in_the_database(web)


# --- 실패: 대상은 그대로, 원인 단계가 남는다 ----------------------------------------------------------
def test_a_database_that_cannot_be_moved_is_found_before_the_target_is_deployed(web):
    deployed_on_aws(web)
    web.transfer.check_result = TransferResult(
        ok=False, error=AdapterError(code="db_too_large", message="DB가 너무 큽니다")
    )
    deploys = len(web.runner.deployer.calls)
    job = finish_job(web, migrate(web, verified(web), "onprem"))
    assert (
        job["status"] == "failed"
        and job["stage"] == "preflight"
        and job["result"]["error"]["code"] == "db_too_large"
    )
    assert len(web.runner.deployer.calls) == deploys and [c[0] for c in web.transfer.calls] == [
        "check"
    ]
    assert target_row(web).onprem_environment_id is None and target_row(web).previous_app_name == ""


def test_a_failed_deploy_on_the_target_stops_before_any_data_moves(web):
    deployed_on_aws(web)
    destination = make_environment(
        web, name="두 번째 AWS", role_arn="arn:aws:iam::223455088214:role/other"
    )
    web.runner.deployer.behaviour = lambda log, secrets: DeployResult(
        ok=False,
        error=AdapterError(code="build_failed", message="빌드 실패"),
        details={"stage": "build"},
    )
    job = finish_job(web, migrate(web, destination, "aws-always-on"))
    assert (
        job["status"] == "failed"
        and job["stage"] == "build"
        and job["result"]["details"]["target_left"] is False
    )
    assert [c[0] for c in web.transfer.calls] == ["check"]
    assert target_row(web).previous_app_name == ""


def test_a_failed_transfer_keeps_the_project_on_its_environment_and_says_the_target_app_was_left(
    web,
):
    aws = deployed_on_aws(web)
    web.transfer.transfer_result = TransferResult(
        ok=False,
        error=AdapterError(code="verify_mismatch", message="행 수가 다릅니다", retryable=True),
        details={"tables": ["notes"]},
    )
    job = finish_job(web, migrate(web, verified(web), "onprem"))
    assert job["status"] == "failed" and job["stage"] == "transfer"
    assert (
        job["result"]["error"]["code"] == "verify_mismatch"
        and job["result"]["details"]["target_left"] is True
    )
    row = target_row(web)
    assert (row.aws_environment_id, row.previous_app_name) == (
        aws,
        "",
    )  # 대상 교체도, 이전 기록도 없다


def test_secrets_never_reach_logs_results_or_the_database_even_when_the_migration_fails(web):
    deployed_on_aws(web)
    destination = make_environment(
        web, name="두 번째 AWS", role_arn="arn:aws:iam::223455088214:role/other"
    )

    def leaky(log, secrets):
        log(LogEvent(message=f"토큰 {secrets['APP_TOKEN']}"))
        return DeployResult(
            ok=False,
            error=AdapterError(code="deploy_failed", message=f"실패 {secrets['APP_TOKEN']}"),
            details={"stage": "deploy"},
        )

    web.runner.deployer.behaviour = leaky
    accepted = migrate(web, destination, "aws-always-on", secrets={"APP_TOKEN": USER_SECRET})
    job = finish_job(web, accepted)
    assert job["status"] == "failed"
    assert USER_SECRET not in json.dumps(job) and USER_SECRET not in accepted.text
    assert USER_SECRET not in everything_in_the_database(web)


@pytest.mark.parametrize("where", ["check", "transfer"])
def test_an_unexpected_error_is_reported_by_kind_only_and_the_project_stays_put(web, where):
    aws = deployed_on_aws(web)
    web.transfer.raise_in = where
    job = finish_job(web, migrate(web, verified(web), "onprem"))
    assert job["status"] == "failed" and job["result"]["error"]["code"] == "migration_runner_error"
    assert job["result"]["details"] == {
        "exception": "RuntimeError"
    } and USER_SECRET not in json.dumps(job)
    assert target_row(web).aws_environment_id == aws


def test_an_environment_that_loses_its_connection_before_the_job_runs_fails_at_the_environment_stage(
    web,
):
    deployed_on_aws(web)
    destination = verified(web)
    accepted = migrate(web, destination, "onprem")
    assert accepted.status_code == 202
    with web.sessions() as session:
        session.get(
            OnpremEnvironment, destination
        ).status = "SIGNALED"  # 작업이 시작되기 전에 연결이 취소됐다
        session.commit()
    web.executor.run_all()
    job = web.client.get(web.endpoint + "/jobs/" + accepted.json()["id"]).json()
    assert job["status"] == "failed" and job["stage"] == "environment" and web.transfer.calls == []


# --- 요청을 받을 때의 거절 --------------------------------------------------------------------------
def test_nothing_can_be_moved_before_a_first_deployment(web):
    identifier = make_environment(web)
    put_target(web, identifier)
    refused = migrate(web, verified(web), "onprem")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "not_deployed"


def test_the_same_environment_is_refused(web):
    aws = deployed_on_aws(web)
    refused = migrate(web, aws, "aws-always-on")
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "same_environment"


def test_an_unknown_or_unconnected_destination_is_refused_without_creating_a_job(web):
    deployed_on_aws(web)
    jobs = len(web.client.get(web.endpoint + "/jobs").json())
    assert migrate(web, str(uuid.uuid4()), "onprem").status_code == 404
    pending = make_environment(web, status="PENDING", role_arn=None, aws_account_id=None)
    refused = migrate(web, pending, "aws-always-on")
    assert (
        refused.status_code == 409
        and refused.json()["detail"]["code"] == "environment_not_connected"
    )
    unverified = web.client.post(
        ENDPOINT,
        headers=web.headers,
        json={"request_id": str(uuid.uuid4()), "name": "미확인", "email": "ops@example.com"},
    ).json()["environment"]["id"]
    assert migrate(web, unverified, "onprem").status_code == 409
    assert len(web.client.get(web.endpoint + "/jobs").json()) == jobs and web.transfer.calls == []


def test_the_target_must_be_given_and_only_a_migration_takes_one(web):
    deployed_on_aws(web)
    assert job_request(web, {"action": "migrate"}).status_code == 422
    assert (
        job_request(
            web, {"action": "migrate", "target_environment_id": str(uuid.uuid4())}
        ).status_code
        == 422
    )
    assert (
        job_request(
            web,
            {
                "action": "destroy",
                "target_environment_id": str(uuid.uuid4()),
                "target_set_name": "onprem",
            },
        ).status_code
        == 422
    )
    assert (
        job_request(web, {"action": "destroy_previous", "secrets": {"A": "b"}}).status_code == 422
    )


def test_a_changed_app_name_is_refused(web):
    deployed_on_aws(web)
    original = web.runner.source.fetch

    def renamed(*args):
        result = original(*args)
        return result.__class__(
            path=result.path,
            commit_sha=result.commit_sha,
            spec={**result.spec, "app": "other"},
            cleanup=result.cleanup,
        )

    web.runner.source.fetch = renamed
    refused = migrate(web, verified(web), "onprem")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "app_name_changed"


def test_an_app_name_used_by_another_project_in_the_destination_is_refused(web):
    deployed_on_aws(web)
    destination = verified(web)
    with web.sessions() as session:
        session.add(
            __import__("app.db", fromlist=["Project"]).Project(
                id="other",
                workspace_id=web.client.get("/api/me").json()["workspace"]["id"],
                repository_id=2,
                installation_id=1,
                full_name="o/other",
                branch="main",
                base_sha="b" * 40,
                created_by=session.scalar(select(__import__("app.db", fromlist=["User"]).User.id)),
                created_at=1,
            )
        )
        session.flush()
        session.add(
            Deployment(
                project_id="other",
                onprem_environment_id=destination,
                set_name="onprem",
                app_name="todo",
                image_tag="aaaaaaa",
            )
        )
        session.commit()
    refused = migrate(web, destination, "onprem")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "app_name_in_use"


def test_a_second_migration_waits_until_the_stopped_app_is_removed(web):
    deployed_on_aws(web)
    finish_job(web, migrate(web, verified(web), "onprem"))
    other = make_environment(web, name="세 번째", role_arn="arn:aws:iam::223455088214:role/third")
    refused = migrate(web, other, "aws-always-on")
    assert (
        refused.status_code == 409 and refused.json()["detail"]["code"] == "previous_app_remaining"
    )


def test_the_same_request_runs_once_and_a_busy_project_refuses_a_second_job(web):
    deployed_on_aws(web)
    destination = verified(web)
    body = {
        "request_id": str(uuid.uuid4()),
        "action": "migrate",
        "target_environment_id": destination,
        "target_set_name": "onprem",
    }
    first = web.client.post(web.endpoint + "/jobs", headers=web.headers, json=body)
    again = web.client.post(web.endpoint + "/jobs", headers=web.headers, json=body)
    assert (
        first.status_code == 202
        and again.status_code == 200
        and first.json()["id"] == again.json()["id"]
    )
    busy = migrate(web, destination, "onprem")
    assert busy.status_code == 409 and busy.json()["detail"]["code"] in (
        "deploy_job_running",
        "environment_busy",
    )
    web.executor.run_all()
    assert [c[0] for c in web.transfer.calls].count("transfer") == 1


def test_an_onprem_destination_that_is_busy_refuses_the_migration_and_creates_no_job(web):
    deployed_on_aws(web)
    destination = verified(web)
    assert (
        environment_job(web, destination, "check").status_code == 202
    )  # 환경의 다른 작업이 선점 중이다
    jobs = len(web.client.get(web.endpoint + "/jobs").json())
    refused = migrate(web, destination, "onprem")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "environment_busy"
    assert len(web.client.get(web.endpoint + "/jobs").json()) == jobs


def test_a_runner_without_migration_support_refuses_the_action(web):
    deployed_on_aws(web)
    web.runner.migration = None
    refused = migrate(web, verified(web), "onprem")
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "action_not_supported"


# --- 이전 앱 삭제 -------------------------------------------------------------------------------
def migrated(web):
    aws = deployed_on_aws(web)
    destination = verified(web)
    assert finish_job(web, migrate(web, destination, "onprem"))["status"] == "succeeded"
    return aws, destination


def test_the_stopped_app_on_the_previous_environment_can_be_removed_after_checking(web):
    migrated(web)
    job = finish_job(web, job_request(web, {"action": "destroy_previous"}))
    assert job["status"] == "succeeded", job
    call = web.transfer.calls[-1]
    assert call[0] == "destroy" and call[2:] == ("todo", "aws-always-on")
    row = target_row(web)
    assert (
        row.previous_kind,
        row.previous_environment_id,
        row.previous_app_name,
        row.previous_url,
    ) == ("", "", "", "")
    assert row.set_name == "onprem" and row.app_name == "todo"  # 지금 앱은 그대로다
    assert web.client.get(web.endpoint).json()["target"]["previous"] is None


def test_a_failed_removal_keeps_the_record_so_it_can_be_tried_again(web):
    migrated(web)
    web.transfer.destroy_result = DestroyResult(
        ok=False, error=AdapterError(code="destroy_failed", message="지우지 못함")
    )
    job = finish_job(web, job_request(web, {"action": "destroy_previous"}))
    assert job["status"] == "failed" and job["stage"] == "destroy"
    assert target_row(web).previous_app_name == "todo"
    web.transfer.destroy_result = None
    assert (
        finish_job(web, job_request(web, {"action": "destroy_previous"}))["status"] == "succeeded"
    )


def test_there_is_nothing_to_remove_when_nothing_was_left_behind(web):
    deployed_on_aws(web)
    refused = job_request(web, {"action": "destroy_previous"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "no_previous_app"


def test_an_unexpected_error_while_removing_is_reported_by_kind_only(web):
    migrated(web)
    web.transfer.raise_in = "destroy"
    job = finish_job(web, job_request(web, {"action": "destroy_previous"}))
    assert job["status"] == "failed" and job["result"]["error"]["code"] == "migration_runner_error"
    assert USER_SECRET not in json.dumps(job) and target_row(web).previous_app_name == "todo"


# --- 남은 앱을 잊지 않는다 -----------------------------------------------------------------------
def test_the_stopped_app_blocks_deleting_the_project_and_cleaning_up_its_environment(web):
    aws, destination = migrated(web)
    deleted = web.client.delete(f"/api/projects/{web.project['id']}", headers=web.headers)
    assert deleted.status_code == 409
    assert (
        environment_job(web, destination, "remove_environment").status_code == 409
    )  # 지금 앱이 쓰는 환경이다
    finish_job(web, job_request(web, {"action": "destroy_previous"}))
    assert target_row(web).previous_app_name == ""


def test_deleting_a_project_is_refused_while_a_stopped_app_is_left_on_its_previous_environment(web):
    # 지금 대상이 AWS이고 멈춘 앱은 온프레미스에 있다. 지금 대상 쪽 규칙만으로는 프로젝트를 지울 수 있는 상태다.
    deployed_on_onprem(web)
    assert (
        finish_job(web, migrate(web, make_environment(web, name="새 AWS"), "aws-always-on"))[
            "status"
        ]
        == "succeeded"
    )
    refused = web.client.delete(f"/api/projects/{web.project['id']}", headers=web.headers)
    assert (
        refused.status_code == 409 and refused.json()["detail"]["code"] == "previous_app_remaining"
    )
    finish_job(web, job_request(web, {"action": "destroy_previous"}))
    assert web.client.delete(
        f"/api/projects/{web.project['id']}", headers=web.headers
    ).status_code in (200, 204)


def test_an_onprem_environment_holding_a_stopped_app_cannot_be_cleaned_up(web):
    onprem = deployed_on_onprem(web)
    aws = make_environment(web, name="새 AWS")
    assert finish_job(web, migrate(web, aws, "aws-always-on"))["status"] == "succeeded"
    assert target_row(web).previous_environment_id == onprem
    refused = environment_job(web, onprem, "remove_environment")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "environment_in_use"
    finish_job(web, job_request(web, {"action": "destroy_previous"}))
    assert environment_job(web, onprem, "remove_environment").status_code == 202


# --- 뒷정리 ------------------------------------------------------------------------------------
def test_the_fetched_source_is_removed_after_success_failure_and_refusal(web):
    deployed_on_aws(web)
    destination = verified(web)
    source = web.runner.source
    assert source.cleanups == len(source.calls)  # 처음 배포가 받은 소스는 이미 지워졌다
    finish_job(web, migrate(web, destination, "onprem"))  # 성공
    assert source.cleanups == len(source.calls)
    finish_job(web, job_request(web, {"action": "destroy_previous"}))
    web.transfer.transfer_result = TransferResult(
        ok=False, error=AdapterError(code="restore_failed", message="실패")
    )
    other = make_environment(web, name="두 번째", role_arn="arn:aws:iam::223455088214:role/other")
    assert (
        finish_job(web, migrate(web, other, "aws-always-on"))["status"] == "failed"
    )  # 데이터 이전 실패
    assert source.cleanups == len(source.calls)
    original = source.fetch

    def renamed(*args):
        result = original(*args)
        return result.__class__(
            path=result.path,
            commit_sha=result.commit_sha,
            spec={**result.spec, "app": "other"},
            cleanup=result.cleanup,
        )

    source.fetch = renamed
    assert (
        migrate(web, other, "aws-always-on").status_code == 409
    )  # 소스를 받은 뒤의 거절(앱 이름 변경)
    assert source.cleanups == len(source.calls)


def test_the_foundation_values_of_a_new_aws_destination_are_saved(web):
    deployed_on_onprem(web)
    aws = make_environment(web, name="새 AWS")
    assert finish_job(web, migrate(web, aws, "aws-always-on"))["status"] == "succeeded"
    with web.sessions() as session:
        row = session.get(AwsEnvironment, aws)
        assert (row.host, row.db_address) == (FOUNDATION["host"], FOUNDATION["db_address"])

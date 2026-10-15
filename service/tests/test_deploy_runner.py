import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from anyship_adapters import AdapterError, DeployResult, DestroyResult, LogEvent
from app.db import AwsEnvironment, Base, DeployJob, Deployment, Project, User, Workspace, database
from app.deploy_runner import FLUSH_EVENTS, FLUSH_SECONDS, DeployRunner, LogSink
from app.deploy_state import DeployStateError
from app.deployments import DeploymentError, claim, select_target
from app.source import GitHubCloneSource, LocalFolderSource, SourceError, SourceResult

ROLE = "arn:aws:iam::223455088214:role/deploy-service-role"
BUCKET = "anyship-tfstate-223455088214-ap-northeast-2-2b9b6060"
SHA = "abc1234def56"
USER_SECRET = "hunter2-user-secret-value"
FOUNDATION = dict(
    host="43.201.158.8",
    db_address="anyship-test-db.abc.ap-northeast-2.rds.amazonaws.com",
    db_port=5432,
    db_secret_arn="arn:aws:secretsmanager:ap-northeast-2:223455088214:secret:rds!db-abc-Xy1",
)
URL = "https://todo.test.aws.anyship.cloud"


class SyncExecutor:
    def submit(self, function, *args):
        function(*args)

    def shutdown(self, **kwargs):
        pass


class DeferredExecutor:
    """작업을 바로 실행하지 않고 모아 둔다(진행 중인 상태를 시험하려고)."""

    def __init__(self):
        self.pending = []

    def submit(self, function, *args):
        self.pending.append((function, args))

    def run_all(self):
        while self.pending:
            function, args = self.pending.pop(0)
            function(*args)

    def shutdown(self, **kwargs):
        pass


class ClosedExecutor(SyncExecutor):
    def submit(self, function, *args):
        raise RuntimeError("cannot schedule new futures after shutdown")


class FakeSource:
    """받은 인자를 기록하고, 받아 둔 임시 소스를 지운 횟수(cleanups)를 센다."""

    def __init__(self, error=None):
        self.error, self.calls, self.cleanups, self.stale_cleared = error, [], 0, 0

    def clear_stale(self):
        self.stale_cleared += 1

    def fetch(self, full_name, branch="", token=None):
        self.calls.append((full_name, branch, token))
        if self.error:
            raise self.error
        return SourceResult(
            path=__import__("pathlib").Path("/srv/sources/todo"),
            commit_sha=SHA,
            spec={"app": "todo"},
            cleanup=self._cleanup,
        )

    def _cleanup(self):
        self.cleanups += 1


class FakeDeployer:
    def __init__(self, behaviour=None, destroy_behaviour=None):
        self.calls, self.behaviour = [], behaviour
        self.destroy_calls, self.destroy_behaviour = [], destroy_behaviour

    def destroy(self, env, app, log, *, set_name):
        self.destroy_calls.append(dict(env=env, app=app, set_name=set_name))
        if self.destroy_behaviour:
            return self.destroy_behaviour(log)
        log(LogEvent(message="컨테이너와 볼륨 제거", step=1, total=1, name="앱 삭제"))
        return DestroyResult(ok=True)

    def deploy(self, env, spec, source_dir, commit_sha, secrets, log, *, set_name):
        self.calls.append(
            dict(
                env=env,
                spec=spec,
                source_dir=source_dir,
                commit_sha=commit_sha,
                secrets=secrets,
                set_name=set_name,
            )
        )
        if self.behaviour:
            return self.behaviour(log, secrets)
        log(LogEvent(message="빌드", step=1, total=5, name="이미지 빌드"))
        return DeployResult(
            ok=True,
            url=URL,
            image_tag=commit_sha,
            details={"foundation": FOUNDATION, "foundation_created": False},
        )


class FakeAccess:
    def __init__(self, bucket=BUCKET, error=None):
        self.bucket, self.error, self.calls = bucket, error, 0

    def read_state_bucket(self, env, stack_name):
        self.calls += 1
        if self.error:
            raise self.error
        return self.bucket


@pytest.fixture
def sessions(database_url):
    engine, factory = database(database_url)
    Base.metadata.create_all(engine)
    with factory() as session:
        session.add_all(
            [User(id="u1", github_id=1, login="u", name="사용자"), Workspace(id="w1", name="w")]
        )
        session.flush()
        session.add(
            Project(
                id="p1",
                workspace_id="w1",
                repository_id=1,
                installation_id=1,
                full_name="o/todo",
                branch="main",
                base_sha="a" * 40,
                created_by="u1",
                created_at=1,
            )
        )
        session.commit()
    yield factory
    engine.dispose()


def make_environment(sessions, **override):
    identifier = str(uuid.uuid4())
    values = dict(
        id=identifier,
        workspace_id="w1",
        created_by="u1",
        request_id=identifier,
        name="연결",
        region="ap-northeast-2",
        external_id=identifier.replace("-", "") * 2,
        template_url="https://bucket.s3.amazonaws.com/a.yaml",
        service_role_arn="arn:aws:iam::9:role/s",
        stack_name="anyship-onboarding-x",
        role_name="deploy-service-role",
        status="CONNECTED",
        role_arn=ROLE,
        aws_account_id="223455088214",
        created_at=1,
        expires_at=2,
        env_id="test",
        state_bucket=BUCKET,
    )
    with sessions() as session:
        session.add(AwsEnvironment(**{**values, **override}))
        session.commit()
    return identifier


def build(
    sessions,
    tmp_path,
    database_url,
    *,
    select=True,
    executor=None,
    source=None,
    deployer=None,
    access=None,
    **environment,
):
    environment_id = make_environment(sessions, **environment)
    if select:
        with sessions() as session:
            select_target(
                session, "p1", session.get(AwsEnvironment, environment_id), "aws-always-on"
            )
    runner = DeployRunner(
        SimpleNamespace(database_url=database_url),
        sessions,
        source=source or FakeSource(),
        deployer=deployer or FakeDeployer(),
        access=access or FakeAccess(),
        executor=executor or SyncExecutor(),
        lock_dir=tmp_path / "lock",
    )
    runner.start()
    runner.executor = executor or runner.executor
    return runner, environment_id


def submit_destroy(sessions, runner, request=None):
    with sessions() as session:
        job, created = runner.submit_destroy(
            session, session.get(Project, "p1"), request or uuid.uuid4()
        )
        return job.id, created


def submit(sessions, runner, request=None, secrets=None, token=None):
    with sessions() as session:
        project = session.get(Project, "p1")
        job, created = runner.submit(
            session, project, request or uuid.uuid4(), secrets or {}, token=token
        )
        return job.id, created


def job_row(sessions, job_id):
    with sessions() as session:
        return session.get(DeployJob, job_id)


def deployment(sessions):
    with sessions() as session:
        return session.get(Deployment, "p1")


def everything_in_the_database(sessions):
    with sessions() as session:
        return json.dumps(
            [
                [getattr(row, c.name) for c in row.__table__.columns]
                for model in (DeployJob, Deployment, AwsEnvironment)
                for row in session.scalars(select(model))
            ],
            default=str,
            ensure_ascii=False,
        )


# --- 성공 ----------------------------------------------------------------------------------
def test_a_deploy_runs_the_deployer_and_records_the_outcome(sessions, tmp_path, database_url):
    deployer = FakeDeployer()
    runner, environment_id = build(sessions, tmp_path, database_url, deployer=deployer)
    job_id, created = submit(sessions, runner, secrets={"API_KEY": USER_SECRET})
    row, held = job_row(sessions, job_id), deployment(sessions)
    assert created and row.status == "succeeded" and row.image_tag == SHA and row.stage == ""
    assert json.loads(row.result_json)["ok"] is True
    assert (held.image_tag, held.url, held.app_name, held.active_job_id) == (SHA, URL, "todo", None)
    (call,) = deployer.calls
    assert (
        call["env"].env_id,
        call["env"].state_bucket,
        call["spec"],
        call["commit_sha"],
        call["set_name"],
    ) == ("test", BUCKET, {"app": "todo"}, SHA, "aws-always-on")
    assert call["secrets"] == {"API_KEY": USER_SECRET}
    assert [e["message"] for e in json.loads(row.logs_json)] == ["빌드"]


def test_the_foundation_values_of_a_successful_deploy_are_saved_on_the_environment(
    sessions, tmp_path, database_url
):
    runner, environment_id = build(sessions, tmp_path, database_url)
    submit(sessions, runner)
    with sessions() as session:
        saved = session.get(AwsEnvironment, environment_id)
        assert {name: getattr(saved, name) for name in FOUNDATION} == FOUNDATION


def test_a_running_job_is_marked_running_while_the_deployer_works(sessions, tmp_path, database_url):
    seen = []

    def behaviour(log, secrets):
        with sessions() as session:
            seen.append(session.scalar(select(DeployJob.status)))
        return DeployResult(ok=True, url=URL, image_tag=SHA)

    runner, _ = build(sessions, tmp_path, database_url, deployer=FakeDeployer(behaviour))
    submit(sessions, runner)
    assert seen == ["running"]


# --- 실패 ----------------------------------------------------------------------------------
def test_a_failed_deploy_records_the_stage_and_keeps_what_was_running(
    sessions, tmp_path, database_url
):
    runner, _ = build(sessions, tmp_path, database_url)
    first, _ = submit(sessions, runner)

    def fails(log, secrets):
        return DeployResult(
            ok=False,
            error=AdapterError(code="docker_build_failed", message="빌드 실패"),
            image_tag=SHA,
            details={"stage": "build", "stderr": "boom"},
        )

    runner.deployer = FakeDeployer(fails)
    second, _ = submit(sessions, runner)
    row, held = job_row(sessions, second), deployment(sessions)
    assert (row.status, row.stage) == ("failed", "build") and json.loads(row.result_json)["error"][
        "code"
    ] == "docker_build_failed"
    assert held.image_tag == SHA and held.active_job_id is None


def test_an_unexpected_exception_is_reported_without_its_text(sessions, tmp_path, database_url):
    def explodes(log, secrets):
        raise RuntimeError(f"leaked {USER_SECRET}")

    runner, _ = build(sessions, tmp_path, database_url, deployer=FakeDeployer(explodes))
    job_id, _ = submit(sessions, runner, secrets={"API_KEY": USER_SECRET})
    row = job_row(sessions, job_id)
    result = json.loads(row.result_json)
    assert (row.status, row.stage, result["error"]["code"]) == (
        "failed",
        "runner",
        "deploy_runner_error",
    )
    assert result["details"][
        "exception"
    ] == "RuntimeError" and USER_SECRET not in everything_in_the_database(sessions)
    assert deployment(sessions).active_job_id is None


def test_user_secrets_are_masked_in_logs_and_results_and_never_stored(
    sessions, tmp_path, database_url
):
    def leaks(log, secrets):
        log(LogEvent(message=f"connecting with {USER_SECRET}"))
        return DeployResult(
            ok=False,
            error=AdapterError(code="leak_test", message=f"bad {USER_SECRET}"),
            details={"stage": "deploy", "note": f"token={USER_SECRET}"},
        )

    runner, _ = build(sessions, tmp_path, database_url, deployer=FakeDeployer(leaks))
    job_id, _ = submit(sessions, runner, secrets={"API_KEY": USER_SECRET})
    assert USER_SECRET not in everything_in_the_database(sessions)
    assert json.loads(job_row(sessions, job_id).logs_json)[0]["message"] == "connecting with ***"


def test_an_environment_that_cannot_prepare_its_state_bucket_fails_before_the_deployer(
    sessions, tmp_path, database_url
):
    deployer = FakeDeployer()
    access = FakeAccess(error=DeployStateError("stack_not_found", "스택을 찾을 수 없습니다."))
    runner, _ = build(
        sessions, tmp_path, database_url, deployer=deployer, access=access, state_bucket=None
    )
    job_id, _ = submit(sessions, runner)
    row = job_row(sessions, job_id)
    assert (row.status, row.stage) == ("failed", "environment") and deployer.calls == []
    assert (
        json.loads(row.result_json)["error"]["code"] == "stack_not_found"
        and deployment(sessions).active_job_id is None
    )


def test_a_missing_state_bucket_is_read_once_and_stored(sessions, tmp_path, database_url):
    access = FakeAccess()
    runner, environment_id = build(
        sessions, tmp_path, database_url, access=access, state_bucket=None
    )
    submit(sessions, runner)
    assert access.calls == 1
    with sessions() as session:
        assert session.get(AwsEnvironment, environment_id).state_bucket == BUCKET


def test_a_stored_state_bucket_means_no_aws_call(sessions, tmp_path, database_url):
    access = FakeAccess()
    runner, _ = build(sessions, tmp_path, database_url, access=access)
    submit(sessions, runner)
    assert access.calls == 0


# --- 요청 안의 검사 ------------------------------------------------------------------------
def test_a_source_error_is_returned_at_once_and_creates_no_job(sessions, tmp_path, database_url):
    runner, _ = build(
        sessions, tmp_path, database_url, source=FakeSource(SourceError("source_not_found", "없음"))
    )
    with sessions() as session, pytest.raises(SourceError):
        runner.submit(session, session.get(Project, "p1"), uuid.uuid4(), {})
    with sessions() as session:
        assert session.scalar(select(DeployJob.id)) is None
    assert deployment(sessions).active_job_id is None


def test_an_environment_that_is_not_connected_is_refused_at_once(sessions, tmp_path, database_url):
    runner, environment_id = build(sessions, tmp_path, database_url)
    with sessions() as session:
        session.get(AwsEnvironment, environment_id).status = "FAILED"
        session.commit()
    with sessions() as session, pytest.raises(DeployStateError):
        runner.submit(session, session.get(Project, "p1"), uuid.uuid4(), {})


def test_a_deploy_needs_a_target(sessions, tmp_path, database_url):
    runner, _ = build(sessions, tmp_path, database_url, select=False)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        runner.submit(session, session.get(Project, "p1"), uuid.uuid4(), {})
    assert caught.value.code == "target_required"


# --- 동시성과 멱등 -------------------------------------------------------------------------
def test_the_same_request_runs_only_once(sessions, tmp_path, database_url):
    deployer = FakeDeployer()
    runner, _ = build(sessions, tmp_path, database_url, deployer=deployer)
    request = uuid.uuid4()
    first, created = submit(sessions, runner, request)
    second, created_again = submit(sessions, runner, request)
    assert (first == second, created, created_again, len(deployer.calls)) == (True, True, False, 1)


def test_a_second_deploy_is_refused_while_the_first_is_queued_and_works_after_it_finishes(
    sessions, tmp_path, database_url
):
    executor = DeferredExecutor()
    runner, _ = build(sessions, tmp_path, database_url, executor=executor)
    first, _ = submit(sessions, runner)
    assert job_row(sessions, first).status == "queued"
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        runner.submit(session, session.get(Project, "p1"), uuid.uuid4(), {})
    assert caught.value.code == "deploy_job_running"
    executor.run_all()
    assert job_row(sessions, first).status == "succeeded" and submit(sessions, runner)[1]


def test_the_lease_is_extended_while_logs_are_written(sessions, tmp_path, database_url):
    executor = DeferredExecutor()
    runner, _ = build(
        sessions,
        tmp_path,
        database_url,
        executor=executor,
        deployer=FakeDeployer(
            lambda log, secrets: (
                [log(LogEvent(message=f"line {n}")) for n in range(FLUSH_EVENTS)],
                DeployResult(
                    ok=False,
                    error=AdapterError(code="log_test", message="m"),
                    details={"stage": "deploy"},
                ),
            )[1]
        ),
    )
    job_id, _ = submit(sessions, runner)
    with sessions() as session:
        session.get(Deployment, "p1").lease_until = 1
        session.commit()
    seen = []
    original = runner._end
    runner._end = lambda *a, **k: (
        seen.append(deployment(sessions).lease_until),
        original(*a, **k),
    )[1]
    executor.run_all()
    assert seen[0] > 1 and len(json.loads(job_row(sessions, job_id).logs_json)) == FLUSH_EVENTS


# --- 시작과 종료 ---------------------------------------------------------------------------
def test_starting_interrupts_jobs_left_by_a_previous_process_and_frees_the_project(
    sessions, tmp_path, database_url
):
    runner, _ = build(sessions, tmp_path, database_url)
    runner.close()  # 이전 프로세스가 이미 끝난 상태
    with sessions() as session:  # 정상 종료하지 못해 선점이 남아 있다
        job, _ = claim(session, "p1", uuid.uuid4(), "old-runtime")
        job_id = job.id
    runner.start()
    assert job_row(sessions, job_id).status == "interrupted"
    assert json.loads(job_row(sessions, job_id).result_json)["error"]["code"] == "worker_restarted"
    assert deployment(sessions).active_job_id is None


def test_only_one_process_may_run_deployments_per_database(sessions, tmp_path, database_url):
    first, _ = build(sessions, tmp_path, database_url)
    second = DeployRunner(
        SimpleNamespace(database_url=database_url),
        sessions,
        source=FakeSource(),
        deployer=FakeDeployer(),
        lock_dir=tmp_path / "lock",
    )
    with pytest.raises(RuntimeError, match="one Service process"):
        second.start()
    first.close()
    second.start()
    second.close()


def test_closing_interrupts_the_jobs_that_were_still_running(sessions, tmp_path, database_url):
    executor = DeferredExecutor()
    runner, _ = build(sessions, tmp_path, database_url, executor=executor)
    job_id, _ = submit(sessions, runner)
    runner.close()
    row = job_row(sessions, job_id)
    assert (
        row.status == "interrupted"
        and json.loads(row.result_json)["error"]["code"] == "worker_stopping"
    )
    assert deployment(sessions).active_job_id is None


def test_a_job_interrupted_before_its_thread_starts_is_never_deployed(
    sessions, tmp_path, database_url
):
    executor, deployer = DeferredExecutor(), FakeDeployer()
    runner, _ = build(sessions, tmp_path, database_url, executor=executor, deployer=deployer)
    job_id, _ = submit(sessions, runner)
    runner.close()  # 대기 중이던 작업이 중단 처리된다
    executor.run_all()  # 그런데 스레드가 뒤늦게 실행된다
    assert deployer.calls == [] and job_row(sessions, job_id).status == "interrupted"


def test_a_request_during_shutdown_fails_the_job_and_frees_the_project(
    sessions, tmp_path, database_url
):
    runner, _ = build(sessions, tmp_path, database_url, executor=ClosedExecutor())
    job_id, _ = submit(sessions, runner)
    row = job_row(sessions, job_id)
    assert (row.status, row.stage) == ("failed", "runner") and deployment(
        sessions
    ).active_job_id is None


# --- 로그 모으기 ---------------------------------------------------------------------------
def event(n=0):
    return LogEvent(message=f"line {n}", data={"token": "x"})


def test_logs_are_written_in_batches_and_the_rest_on_flush():
    written = []
    sink = LogSink(written.append)
    for n in range(FLUSH_EVENTS - 1):
        sink.add(event(n))
    assert written == []
    sink.add(event(99))
    assert len(written) == 1 and len(written[0]) == FLUSH_EVENTS
    sink.add(event(100))
    sink.flush()
    assert len(written) == 2 and written[1][0]["message"] == "line 100"


def test_logs_are_written_after_a_while_even_if_few():
    now = [0.0]
    written = []
    sink = LogSink(written.append, clock=lambda: now[0])
    sink.add(event(1))
    now[0] = FLUSH_SECONDS + 0.1
    sink.add(event(2))
    assert len(written) == 1 and len(written[0]) == 2


def test_a_failed_write_keeps_the_events_for_the_next_try():
    attempts, written = [], []

    def flaky(events):
        attempts.append(len(events))
        if len(attempts) == 1:
            raise OSError("database hiccup")
        written.append(events)

    sink = LogSink(flaky)
    sink.add(event(1))
    sink.flush()
    sink.add(event(2))
    sink.flush()
    assert attempts == [1, 2] and [e["message"] for e in written[0]] == ["line 1", "line 2"]


def test_diagnostic_data_is_not_copied_into_the_database():
    written = []
    sink = LogSink(written.append)
    sink.add(event(1))
    sink.flush()
    assert "data" not in written[0][0]


def test_known_secrets_are_masked_before_logs_are_written():
    written = []
    sink = LogSink(written.append, {"API_KEY": USER_SECRET})
    sink.add(LogEvent(message=f"using {USER_SECRET}"))
    sink.flush()
    assert written[0][0]["message"] == "using ***"


# --- 조립 ----------------------------------------------------------------------------------
def settings_for(tmp_path, **override):
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "terraform").mkdir(exist_ok=True)
    (tmp_path / "key").write_text("PRIVATE")
    (tmp_path / "key.pub").write_text("ssh-ed25519 AAAA deploy\n")
    values = dict(
        deploy_source="github",
        deploy_workspace=tmp_path / "work",
        deploy_source_dir=tmp_path / "sources",
        deploy_ssh_key=tmp_path / "key",
        deploy_terraform_dir=tmp_path / "terraform",
        deploy_plugin_cache=tmp_path / "cache",
        deploy_base_domain="anyship.cloud",
        deploy_verify_tls=True,
        deploy_dns=True,
        deploy_service_ip="203.0.113.10",
        deploy_acme_email="ops@example.com",
        database_url="sqlite://",
    )
    return SimpleNamespace(**{**values, **override})


def test_the_runner_is_assembled_from_settings(sessions, tmp_path):
    runner = DeployRunner.from_settings(settings_for(tmp_path), sessions)
    assert runner.source and runner.deployer


def test_dns_is_managed_in_the_aws_scope_of_the_configured_domain_by_default(sessions, tmp_path):
    runner = DeployRunner.from_settings(
        settings_for(tmp_path, deploy_base_domain="example.org"), sessions
    )
    dns = runner.deployer._dns
    assert dns is not None and dns.name_for("demo") == "*.demo.aws.example.org"


def test_dns_is_left_alone_when_it_is_switched_off(sessions, tmp_path):
    assert (
        DeployRunner.from_settings(settings_for(tmp_path, deploy_dns=False), sessions).deployer._dns
        is None
    )


@pytest.mark.parametrize(
    "override, message",
    [
        ({"deploy_source": "local", "deploy_source_dir": "missing"}, "APP_DEPLOY_SOURCE_DIR"),
        ({"deploy_ssh_key": "missing"}, "APP_DEPLOY_SSH_KEY"),
        ({"deploy_terraform_dir": "missing"}, "APP_DEPLOY_TERRAFORM_DIR"),
    ],
)
def test_missing_paths_stop_the_server_at_startup_by_name(sessions, tmp_path, override, message):
    settings = settings_for(tmp_path)
    for key, value in override.items():
        setattr(settings, key, value if key == "deploy_source" else tmp_path / value)
    with pytest.raises(RuntimeError, match=message):
        DeployRunner.from_settings(settings, sessions)


def test_the_github_source_is_used_by_default_and_needs_no_source_folder(sessions, tmp_path):
    settings = settings_for(tmp_path, deploy_source_dir=tmp_path / "does-not-exist")
    runner = DeployRunner.from_settings(settings, sessions)
    assert (
        isinstance(runner.source, GitHubCloneSource)
        and runner.source._workspace == tmp_path / "work"
    )


def test_the_local_folder_source_is_used_only_when_asked_for(sessions, tmp_path):
    runner = DeployRunner.from_settings(settings_for(tmp_path, deploy_source="local"), sessions)
    assert isinstance(runner.source, LocalFolderSource)


@pytest.mark.parametrize("content", [None, "not a key"])
def test_an_unusable_public_key_stops_the_server_without_echoing_it(sessions, tmp_path, content):
    settings = settings_for(tmp_path)
    (tmp_path / "key.pub").unlink()
    if content:
        (tmp_path / "key.pub").write_text(content)
    with pytest.raises(RuntimeError) as caught:
        DeployRunner.from_settings(settings, sessions)
    assert "not a key" not in str(caught.value)


# --- 삭제 ----------------------------------------------------------------------------------
def deployed_runner(sessions, tmp_path, database_url, **options):
    """한 번 배포가 끝난 실행기(현재 버전, 주소, 앱 이름, 기반 값이 저장되어 있다)."""
    runner, environment_id = build(sessions, tmp_path, database_url, **options)
    submit(sessions, runner)
    return runner, environment_id


def test_a_destroy_runs_the_deployer_for_the_deployed_app_and_returns_the_target_to_not_deployed(
    sessions, tmp_path, database_url
):
    deployer = FakeDeployer()
    runner, environment_id = deployed_runner(sessions, tmp_path, database_url, deployer=deployer)
    assert deployment(sessions).image_tag == SHA
    job_id, created = submit_destroy(sessions, runner)
    row, held = job_row(sessions, job_id), deployment(sessions)
    assert created and (row.action, row.status, row.stage, row.image_tag) == (
        "destroy",
        "succeeded",
        "",
        SHA,
    )
    assert json.loads(row.result_json)["ok"] is True
    assert (held.image_tag, held.url, held.app_name, held.active_job_id, held.lease_until) == (
        "",
        "",
        "",
        None,
        0,
    )
    (call,) = deployer.destroy_calls
    assert (call["app"], call["set_name"], call["env"].env_id) == ("todo", "aws-always-on", "test")
    assert call["env"].host == FOUNDATION["host"]  # 배포가 저장해 둔 기반 값으로 호스트에 접속한다
    assert [e["message"] for e in json.loads(row.logs_json)] == ["컨테이너와 볼륨 제거"]


def test_after_a_destroy_the_app_can_be_deployed_again(sessions, tmp_path, database_url):
    runner, _ = deployed_runner(sessions, tmp_path, database_url)
    submit_destroy(sessions, runner)
    again, created = submit(sessions, runner)
    assert (
        created
        and job_row(sessions, again).status == "succeeded"
        and deployment(sessions).image_tag == SHA
    )


def test_a_failed_destroy_records_the_stage_and_keeps_the_deployment_as_it_was(
    sessions, tmp_path, database_url
):
    def fails(log):
        log(LogEvent(level="error", message="컨테이너를 지우지 못했습니다."))
        return DestroyResult(
            ok=False,
            error=AdapterError(
                code="destroy_failed",
                message="컨테이너를 지우지 못했습니다.",
                hint="서버에서 docker compose down을 확인해 주세요.",
            ),
        )

    runner, _ = deployed_runner(
        sessions, tmp_path, database_url, deployer=FakeDeployer(destroy_behaviour=fails)
    )
    job_id, _ = submit_destroy(sessions, runner)
    row, held = job_row(sessions, job_id), deployment(sessions)
    assert (row.status, row.stage) == ("failed", "destroy") and json.loads(row.result_json)[
        "error"
    ]["code"] == "destroy_failed"
    assert (held.image_tag, held.url, held.app_name, held.active_job_id) == (SHA, URL, "todo", None)


def test_an_unexpected_exception_in_a_destroy_is_reported_without_its_text(
    sessions, tmp_path, database_url
):
    def explodes(log):
        raise RuntimeError(f"leaked {USER_SECRET}")

    runner, _ = deployed_runner(
        sessions, tmp_path, database_url, deployer=FakeDeployer(destroy_behaviour=explodes)
    )
    job_id, _ = submit_destroy(sessions, runner)
    row = job_row(sessions, job_id)
    result = json.loads(row.result_json)
    assert (row.status, row.stage, result["error"]["code"]) == (
        "failed",
        "runner",
        "deploy_runner_error",
    )
    assert (
        USER_SECRET not in everything_in_the_database(sessions)
        and deployment(sessions).image_tag == SHA
    )


def test_nothing_is_destroyed_before_a_first_deployment(sessions, tmp_path, database_url):
    deployer = FakeDeployer()
    runner, _ = build(sessions, tmp_path, database_url, deployer=deployer)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        runner.submit_destroy(session, session.get(Project, "p1"), uuid.uuid4())
    assert caught.value.code == "not_deployed" and deployer.destroy_calls == []
    with sessions() as session:
        assert session.scalar(select(DeployJob.id)) is None


def test_a_destroy_needs_a_target(sessions, tmp_path, database_url):
    runner, _ = build(sessions, tmp_path, database_url, select=False)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        runner.submit_destroy(session, session.get(Project, "p1"), uuid.uuid4())
    assert caught.value.code == "target_required"


def test_an_environment_that_is_not_connected_is_refused_before_any_job(
    sessions, tmp_path, database_url
):
    deployer = FakeDeployer()
    runner, environment_id = deployed_runner(sessions, tmp_path, database_url, deployer=deployer)
    with sessions() as session:
        session.get(AwsEnvironment, environment_id).status = "FAILED"
        session.commit()
    with sessions() as session, pytest.raises(DeployStateError):
        runner.submit_destroy(session, session.get(Project, "p1"), uuid.uuid4())
    assert deployer.destroy_calls == [] and deployment(sessions).active_job_id is None


def test_a_deployment_record_without_an_app_name_cannot_be_destroyed(
    sessions, tmp_path, database_url
):
    deployer = FakeDeployer()
    runner, _ = deployed_runner(sessions, tmp_path, database_url, deployer=deployer)
    with sessions() as session:
        session.get(Deployment, "p1").app_name = ""
        session.commit()
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        runner.submit_destroy(session, session.get(Project, "p1"), uuid.uuid4())
    assert caught.value.code == "app_name_missing" and deployer.destroy_calls == []
    assert deployment(sessions).active_job_id is None


def test_the_same_destroy_request_runs_only_once(sessions, tmp_path, database_url):
    deployer = FakeDeployer()
    runner, _ = deployed_runner(sessions, tmp_path, database_url, deployer=deployer)
    request = uuid.uuid4()
    first, created = submit_destroy(sessions, runner, request)
    second, created_again = submit_destroy(sessions, runner, request)
    assert (first == second, created, created_again, len(deployer.destroy_calls)) == (
        True,
        True,
        False,
        1,
    )


def test_a_destroy_waits_for_a_running_deploy(sessions, tmp_path, database_url):
    executor = DeferredExecutor()
    runner, _ = deployed_runner(sessions, tmp_path, database_url, executor=executor)
    executor.run_all()  # 앞서 배포한 작업이 끝나 있다
    submit(sessions, runner)  # 새 배포가 대기 중
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        runner.submit_destroy(session, session.get(Project, "p1"), uuid.uuid4())
    assert caught.value.code == "deploy_job_running"
    executor.run_all()
    assert submit_destroy(sessions, runner)[1]


def test_a_destroy_interrupted_before_its_thread_starts_never_touches_the_host(
    sessions, tmp_path, database_url
):
    executor, deployer = DeferredExecutor(), FakeDeployer()
    runner, _ = deployed_runner(
        sessions, tmp_path, database_url, executor=executor, deployer=deployer
    )
    executor.run_all()
    job_id, _ = submit_destroy(sessions, runner)
    runner.close()
    executor.run_all()
    assert deployer.destroy_calls == [] and job_row(sessions, job_id).status == "interrupted"
    assert deployment(sessions).image_tag == SHA  # 지워진 것이 없으니 상태도 그대로다


def test_a_destroy_during_shutdown_fails_the_job_and_frees_the_project(
    sessions, tmp_path, database_url
):
    runner, _ = deployed_runner(sessions, tmp_path, database_url)
    runner.executor = ClosedExecutor()
    job_id, _ = submit_destroy(sessions, runner)
    row = job_row(sessions, job_id)
    assert (row.status, row.stage) == ("failed", "runner") and deployment(
        sessions
    ).active_job_id is None
    assert deployment(sessions).image_tag == SHA


def test_an_environment_that_stops_being_connected_before_the_thread_runs_fails_the_destroy_without_touching_the_host(
    sessions, tmp_path, database_url
):
    executor, deployer = DeferredExecutor(), FakeDeployer()
    runner, environment_id = deployed_runner(
        sessions, tmp_path, database_url, executor=executor, deployer=deployer
    )
    executor.run_all()
    job_id, _ = submit_destroy(sessions, runner)
    with sessions() as session:
        session.get(AwsEnvironment, environment_id).status = "FAILED"
        session.commit()
    executor.run_all()
    row = job_row(sessions, job_id)
    assert (row.status, row.stage) == ("failed", "environment") and deployer.destroy_calls == []
    assert json.loads(row.result_json)["error"]["code"] == "environment_not_connected"
    assert deployment(sessions).image_tag == SHA and deployment(sessions).active_job_id is None


# --- GitHub에서 받은 소스(토큰과 임시 폴더) ---------------------------------------------------
GITHUB_TOKEN = "gho_" + "t" * 30


def test_the_source_is_fetched_for_the_projects_repository_and_branch_with_the_users_token(
    sessions, tmp_path, database_url
):
    source = FakeSource()
    runner, _ = build(sessions, tmp_path, database_url, source=source)
    submit(sessions, runner, token=GITHUB_TOKEN)
    assert source.calls == [("o/todo", "main", GITHUB_TOKEN)]


def test_the_users_token_never_reaches_the_deployer_the_database_or_the_job_record(
    sessions, tmp_path, database_url
):
    deployer = FakeDeployer()
    runner, _ = build(sessions, tmp_path, database_url, deployer=deployer)
    job_id, _ = submit(sessions, runner, token=GITHUB_TOKEN)
    assert GITHUB_TOKEN not in repr(
        deployer.calls
    ) and GITHUB_TOKEN not in everything_in_the_database(sessions)
    assert job_row(sessions, job_id).status == "succeeded"


@pytest.mark.parametrize("outcome", ["success", "failure", "exception"])
def test_the_received_source_is_removed_after_the_job_however_it_ends(
    sessions, tmp_path, database_url, outcome
):
    source = FakeSource()
    behaviours = {
        "success": None,
        "failure": lambda log, secrets: DeployResult(
            ok=False,
            error=AdapterError(code="docker_build_failed", message="x"),
            details={"stage": "build"},
        ),
        "exception": lambda log, secrets: (_ for _ in ()).throw(RuntimeError("boom")),
    }
    runner, _ = build(
        sessions, tmp_path, database_url, source=source, deployer=FakeDeployer(behaviours[outcome])
    )
    submit(sessions, runner)
    assert len(source.calls) == 1 and source.cleanups == 1


def test_the_source_is_kept_while_the_job_is_queued_and_removed_when_the_thread_has_finished(
    sessions, tmp_path, database_url
):
    source, executor = FakeSource(), DeferredExecutor()
    runner, _ = build(sessions, tmp_path, database_url, source=source, executor=executor)
    submit(sessions, runner)
    assert source.cleanups == 0  # 스레드가 쓸 때까지 지우지 않는다
    executor.run_all()
    assert source.cleanups == 1


def test_a_failed_environment_step_still_removes_the_source(sessions, tmp_path, database_url):
    source, executor = FakeSource(), DeferredExecutor()
    runner, environment_id = build(
        sessions, tmp_path, database_url, source=source, executor=executor
    )
    submit(sessions, runner)
    with sessions() as session:
        session.get(AwsEnvironment, environment_id).status = "FAILED"
        session.commit()
    executor.run_all()
    assert source.cleanups == 1


def test_a_job_interrupted_before_its_thread_starts_still_removes_the_source(
    sessions, tmp_path, database_url
):
    source, executor = FakeSource(), DeferredExecutor()
    runner, _ = build(sessions, tmp_path, database_url, source=source, executor=executor)
    submit(sessions, runner)
    runner.close()
    executor.run_all()
    assert source.cleanups == 1


def test_a_request_that_loses_the_race_for_the_lease_removes_the_source_it_fetched(
    sessions, tmp_path, database_url
):
    source, executor = FakeSource(), DeferredExecutor()
    runner, _ = build(sessions, tmp_path, database_url, source=source, executor=executor)
    submit(sessions, runner)
    with sessions() as session, pytest.raises(DeploymentError):
        runner.submit(session, session.get(Project, "p1"), uuid.uuid4(), {}, token=GITHUB_TOKEN)
    assert (
        len(source.calls) == 2 and source.cleanups == 1
    )  # 두 번째 요청의 소스는 바로 지워졌고 첫 번째는 스레드가 끝나길 기다린다
    executor.run_all()
    assert source.cleanups == 2


def test_a_shutdown_in_progress_removes_the_source_and_frees_the_project(
    sessions, tmp_path, database_url
):
    source = FakeSource()
    runner, _ = build(sessions, tmp_path, database_url, source=source, executor=ClosedExecutor())
    submit(sessions, runner)
    assert source.cleanups == 1 and deployment(sessions).active_job_id is None


def test_a_repeated_request_does_not_fetch_the_source_again(sessions, tmp_path, database_url):
    source = FakeSource()
    runner, _ = build(sessions, tmp_path, database_url, source=source)
    request = uuid.uuid4()
    first, created = submit(sessions, runner, request, token=GITHUB_TOKEN)
    second, created_again = submit(sessions, runner, request, token=GITHUB_TOKEN)
    assert (first == second, created, created_again) == (True, True, False)
    assert len(source.calls) == 1 and source.cleanups == 1


def test_a_source_error_leaves_nothing_to_clean_and_creates_no_job(
    sessions, tmp_path, database_url
):
    source = FakeSource(SourceError("source_fetch_failed", "GitHub에서 소스를 받지 못했습니다."))
    runner, _ = build(sessions, tmp_path, database_url, source=source)
    with sessions() as session, pytest.raises(SourceError) as caught:
        runner.submit(session, session.get(Project, "p1"), uuid.uuid4(), {}, token=GITHUB_TOKEN)
    assert caught.value.code == "source_fetch_failed" and GITHUB_TOKEN not in caught.value.message
    assert source.cleanups == 0 and deployment(sessions).active_job_id is None


def test_starting_clears_temporary_sources_left_by_a_crashed_process(
    sessions, tmp_path, database_url
):
    source = FakeSource()
    runner, _ = build(sessions, tmp_path, database_url, source=source)
    assert source.stale_cleared == 1  # build()가 start()를 부른다
    runner.close()
    runner.start()
    assert source.stale_cleared == 2


def test_a_destroy_never_fetches_a_source(sessions, tmp_path, database_url):
    source = FakeSource()
    runner, _ = build(sessions, tmp_path, database_url, source=source)
    submit(sessions, runner)
    calls_before = len(source.calls)
    submit_destroy(sessions, runner)
    assert len(source.calls) == calls_before


def test_the_second_of_two_simultaneous_identical_requests_removes_the_source_it_fetched(
    sessions, tmp_path, database_url, monkeypatch
):
    # 두 요청이 거의 동시에 들어와 둘 다 "아직 같은 요청이 없다"는 검사를 통과한 상황을 만든다. 선점에서야 중복이 드러난다.
    source = FakeSource()
    runner, _ = build(sessions, tmp_path, database_url, source=source)
    request = uuid.uuid4()
    first, created = submit(sessions, runner, request)
    assert created and source.cleanups == 1
    monkeypatch.setattr("app.deploy_runner.find_request", lambda *args, **kwargs: None)
    second, created_again = submit(sessions, runner, request, token=GITHUB_TOKEN)
    assert (first == second, created_again) == (True, False)
    assert len(source.calls) == 2 and source.cleanups == 2  # 늦게 온 쪽이 받은 소스도 남지 않는다

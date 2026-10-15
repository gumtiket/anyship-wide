import json
import uuid

import pytest
from sqlalchemy import select

from app.db import AwsEnvironment, Base, DeployJob, Deployment, Project, User, Workspace, database
from app.deployments import (
    ACTIVE,
    LEASE_SECONDS,
    MAX_EVENTS,
    MAX_MESSAGE,
    OMITTED,
    DeploymentError,
    add_events,
    append_logs,
    claim,
    extend_lease,
    finish,
    interrupt_all,
    job_json,
    select_target,
)

ROLE = "arn:aws:iam::223455088214:role/deploy-service-role"
NOW = 1_000_000


@pytest.fixture
def sessions(database_url):
    engine, factory = database(database_url)
    Base.metadata.create_all(engine)
    with factory() as session:
        session.add_all(
            [User(id="u1", github_id=1, login="u", name="사용자"), Workspace(id="w1", name="w")]
        )
        session.flush()
        session.add_all(
            Project(
                id=name,
                workspace_id="w1",
                repository_id=number,
                installation_id=1,
                full_name=f"o/{name}",
                branch="main",
                base_sha="a" * 40,
                created_by="u1",
                created_at=1,
            )
            for number, name in enumerate(("p1", "p2"), start=1)
        )
        session.commit()
    yield factory
    engine.dispose()


def add_environment(sessions, number=0, **override):
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
        service_role_arn="arn:aws:iam::999999999999:role/service-server",
        stack_name="s",
        role_name="deploy-service-role",
        status="CONNECTED",
        role_arn=f"{ROLE[:-5]}{number}",
        aws_account_id="223455088214",
        created_at=1,
        expires_at=2,
    )
    with sessions() as session:
        session.add(AwsEnvironment(**{**values, **override}))
        session.commit()
    return identifier


def target(sessions, project="p1", set_name="aws-always-on"):
    environment_id = add_environment(sessions, number=int(uuid.uuid4().int % 10_000))
    with sessions() as session:
        select_target(session, project, session.get(AwsEnvironment, environment_id), set_name)
    return environment_id


def start(sessions, project="p1", request=None, now=NOW):
    with sessions() as session:
        job, created = claim(session, project, request or uuid.uuid4(), "runtime", now=now)
        return job.id, created


def start_destroy(sessions, project="p1", request=None, now=NOW):
    with sessions() as session:
        job, created = claim(
            session, project, request or uuid.uuid4(), "runtime", action="destroy", now=now
        )
        return job.id, created


def deployed_target(sessions, project="p1"):
    """배포까지 끝난 대상(현재 버전이 있다)."""
    environment_id = target(sessions, project)
    job_id, _ = start(sessions, project)
    assert finish_ok(sessions, job_id)
    return environment_id


def deployment(sessions, project="p1"):
    with sessions() as session:
        return session.get(Deployment, project)


def job_row(sessions, identifier):
    with sessions() as session:
        return session.get(DeployJob, identifier)


# --- select_target -------------------------------------------------------------------------
def test_a_connected_environment_becomes_the_target_and_gets_its_env_id(sessions):
    environment_id = add_environment(sessions)
    with sessions() as session:
        saved = select_target(
            session, "p1", session.get(AwsEnvironment, environment_id), "aws-always-on"
        )
        assert (saved.aws_environment_id, saved.set_name, saved.image_tag, saved.active_job_id) == (
            environment_id,
            "aws-always-on",
            "",
            None,
        )
        assert session.get(AwsEnvironment, environment_id).env_id.startswith("e")


@pytest.mark.parametrize(
    "override",
    [
        {"status": "PENDING"},
        {"status": "FAILED"},
        {"status": "EXPIRED"},
        {"role_arn": None, "aws_account_id": None},
    ],
)
def test_an_environment_that_is_not_connected_cannot_be_selected(sessions, override):
    environment_id = add_environment(sessions, **override)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        select_target(session, "p1", session.get(AwsEnvironment, environment_id), "aws-always-on")
    assert caught.value.code == "environment_not_connected" and deployment(sessions) is None


@pytest.mark.parametrize("set_name", ["aws-serverless", "onprem", "nope"])
def test_only_sets_that_can_really_be_deployed_are_accepted(sessions, set_name):
    environment_id = add_environment(sessions)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        select_target(session, "p1", session.get(AwsEnvironment, environment_id), set_name)
    assert (caught.value.code, caught.value.status) == ("set_not_supported", 422) and deployment(
        sessions
    ) is None


def test_selecting_the_same_target_again_changes_nothing(sessions):
    environment_id = target(sessions)
    before = deployment(sessions)
    with sessions() as session:
        again = select_target(
            session, "p1", session.get(AwsEnvironment, before.aws_environment_id), "aws-always-on"
        )
    assert again.aws_environment_id == before.aws_environment_id == environment_id


def test_the_target_can_be_changed_until_something_is_deployed(sessions):
    target(sessions)
    other = add_environment(sessions, number=777)
    with sessions() as session:
        changed = select_target(session, "p1", session.get(AwsEnvironment, other), "aws-always-on")
    assert changed.aws_environment_id == other


def test_the_target_cannot_be_changed_after_a_deployment(sessions):
    target(sessions)
    job_id, _ = start(sessions)
    assert finish_ok(sessions, job_id)
    other = add_environment(sessions, number=778)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        select_target(session, "p1", session.get(AwsEnvironment, other), "aws-always-on")
    assert caught.value.code == "target_in_use"


def test_the_target_cannot_be_changed_while_a_job_is_active(sessions):
    environment_id = target(sessions)
    start(sessions)
    other = add_environment(sessions, number=779)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        select_target(session, "p1", session.get(AwsEnvironment, other), "aws-always-on")
    assert (
        caught.value.code == "target_in_use"
        and deployment(sessions).aws_environment_id == environment_id
    )


def test_selecting_the_same_target_again_after_a_deployment_still_succeeds(sessions):
    environment_id = target(sessions)
    job_id, _ = start(sessions)
    finish_ok(sessions, job_id)
    with sessions() as session:
        again = select_target(
            session, "p1", session.get(AwsEnvironment, environment_id), "aws-always-on"
        )
    assert again.image_tag == "abc1234def56"


# --- claim ---------------------------------------------------------------------------------
def test_claiming_creates_a_queued_job_and_holds_the_lease(sessions):
    target(sessions)
    job_id, created = start(sessions)
    row, held = job_row(sessions, job_id), deployment(sessions)
    assert created and (row.status, row.action, row.set_name, row.runtime_id) == (
        "queued",
        "deploy",
        "aws-always-on",
        "runtime",
    )
    assert (held.active_job_id, held.lease_until) == (
        job_id,
        NOW + LEASE_SECONDS,
    ) and row.created_at == NOW * 1000


def test_the_lease_covers_the_twenty_minute_foundation_apply(sessions):
    assert LEASE_SECONDS >= 25 * 60


def test_a_repeated_request_id_returns_the_same_job_without_touching_the_lease(sessions):
    target(sessions)
    request = uuid.uuid4()
    first, created = start(sessions, request=request)
    second, created_again = start(sessions, request=request, now=NOW + 500)
    assert (first == second, created, created_again) == (True, True, False)
    assert deployment(sessions).lease_until == NOW + LEASE_SECONDS


def test_a_second_request_is_refused_while_the_first_holds_the_lease(sessions):
    target(sessions)
    first, _ = start(sessions)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        claim(session, "p1", uuid.uuid4(), "runtime", now=NOW + LEASE_SECONDS - 1)
    assert caught.value.code == "deploy_job_running" and deployment(sessions).active_job_id == first


def test_an_expired_lease_is_taken_over_and_the_dead_job_is_marked_interrupted(sessions):
    target(sessions)
    dead, _ = start(sessions)
    fresh, created = start(sessions, now=NOW + LEASE_SECONDS)
    old = job_row(sessions, dead)
    assert created and fresh != dead and deployment(sessions).active_job_id == fresh
    assert (
        old.status == "interrupted"
        and json.loads(old.result_json)["error"]["code"] == "lease_expired"
    )


def test_projects_do_not_block_each_other(sessions):
    target(sessions, "p1")
    target(sessions, "p2")
    start(sessions, "p1")
    assert start(sessions, "p2")[1]


def test_a_job_needs_a_target(sessions):
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        claim(session, "p1", uuid.uuid4(), "runtime")
    assert caught.value.code == "target_required"
    assert session.scalar(select(DeployJob.id)) is None


# --- finish --------------------------------------------------------------------------------
def finish_ok(sessions, job_id, **override):
    deployed = {
        "image_tag": "abc1234def56",
        "url": "https://todo.test.aws.anyship.cloud",
        "app_name": "todo",
        **override,
    }
    with sessions() as session:
        return finish(
            session, job_id, ok=True, result={"ok": True}, deployed=deployed, now=NOW + 10
        )


def test_a_successful_job_releases_the_lease_and_records_what_is_running(sessions):
    target(sessions)
    job_id, _ = start(sessions)
    assert finish_ok(sessions, job_id)
    held, row = deployment(sessions), job_row(sessions, job_id)
    assert (held.active_job_id, held.lease_until) == (None, 0)
    assert (held.image_tag, held.url, held.app_name) == (
        "abc1234def56",
        "https://todo.test.aws.anyship.cloud",
        "todo",
    )
    assert (row.status, row.finished_at) == ("succeeded", (NOW + 10) * 1000)


def test_a_failed_job_records_the_stage_and_keeps_what_was_running(sessions):
    target(sessions)
    first, _ = start(sessions)
    finish_ok(sessions, first)
    second, _ = start(sessions)
    with sessions() as session:
        assert finish(
            session,
            second,
            ok=False,
            result={"ok": False, "error": {"code": "x"}},
            stage="build",
            now=NOW,
        )
    held, row = deployment(sessions), job_row(sessions, second)
    assert (row.status, row.stage) == ("failed", "build") and held.active_job_id is None
    assert held.image_tag == "abc1234def56"  # 실패한 배포가 현재 버전을 바꾸지 않는다


def test_a_job_can_be_finished_only_once(sessions):
    target(sessions)
    job_id, _ = start(sessions)
    assert finish_ok(sessions, job_id)
    assert not finish_ok(sessions, job_id, image_tag="ffffffff")
    assert deployment(sessions).image_tag == "abc1234def56"


def test_a_late_finish_of_an_interrupted_job_does_not_clobber_the_new_one(sessions):
    target(sessions)
    dead, _ = start(sessions)
    fresh, _ = start(sessions, now=NOW + LEASE_SECONDS)
    assert not finish_ok(sessions, dead, image_tag="deadbeef")
    held = deployment(sessions)
    assert (
        held.active_job_id == fresh
        and held.image_tag == ""
        and job_row(sessions, dead).status == "interrupted"
    )


def test_finishing_one_project_does_not_release_another_projects_lease(sessions):
    target(sessions, "p1")
    target(sessions, "p2")
    first, _ = start(sessions, "p1")
    second, _ = start(sessions, "p2")
    assert finish_ok(sessions, first)
    assert (
        deployment(sessions, "p2").active_job_id == second
        and deployment(sessions, "p2").lease_until == NOW + LEASE_SECONDS
    )
    assert deployment(sessions, "p2").image_tag == ""


def test_a_failed_job_never_changes_what_is_running_even_if_deployed_values_are_passed(sessions):
    target(sessions)
    job_id, _ = start(sessions)
    deployed = {"image_tag": "abc1234def56", "url": "https://x.example", "app_name": "todo"}
    with sessions() as session:
        assert finish(
            session,
            job_id,
            ok=False,
            result={"ok": False},
            stage="deploy",
            deployed=deployed,
            now=NOW,
        )
    assert (deployment(sessions).image_tag, deployment(sessions).url) == ("", "")


# --- extend_lease, interrupt_all -----------------------------------------------------------
def test_extending_the_lease_moves_it_only_for_the_active_job(sessions):
    target(sessions)
    job_id, _ = start(sessions)
    with sessions() as session:
        extend_lease(session, job_id, now=NOW + 600)
        extend_lease(session, "someone-else", now=NOW + 9999)
    assert deployment(sessions).lease_until == NOW + 600 + LEASE_SECONDS


def test_restart_interrupts_active_jobs_and_frees_every_project(sessions):
    target(sessions, "p1")
    target(sessions, "p2")
    running, _ = start(sessions, "p1")
    done, _ = start(sessions, "p2")
    finish_ok(sessions, done)
    with sessions() as session:
        assert (
            interrupt_all(session, "worker_restarted", "서버가 다시 시작되었습니다.", now=NOW + 5)
            == 1
        )
    assert (
        job_row(sessions, running).status == "interrupted"
        and job_row(sessions, done).status == "succeeded"
    )
    assert json.loads(job_row(sessions, running).result_json)["error"]["code"] == "worker_restarted"
    assert (
        deployment(sessions, "p1").active_job_id is None
        and deployment(sessions, "p1").lease_until == 0
    )
    assert start(sessions, "p1")[1]  # 다음 요청이 막히지 않는다


# --- 로그 ----------------------------------------------------------------------------------
def event(number, message=None):
    return {
        "level": "info",
        "step": 1,
        "total": 5,
        "name": "배포",
        "message": message or f"line {number}",
    }


def test_events_under_the_limit_are_kept_in_order():
    merged = add_events([event(1)], [event(2), event(3)])
    assert [e["message"] for e in merged] == ["line 1", "line 2", "line 3"]


def test_events_over_the_limit_keep_the_start_and_the_newest_with_one_omission_marker():
    merged = add_events([], [event(n) for n in range(MAX_EVENTS + 50)])
    assert len(merged) == MAX_EVENTS and [e["message"] for e in merged].count(OMITTED) == 1
    assert merged[0]["message"] == "line 0" and merged[-1]["message"] == f"line {MAX_EVENTS + 49}"


def test_adding_after_the_cut_keeps_one_marker_and_the_latest_event():
    merged = add_events([], [event(n) for n in range(MAX_EVENTS + 10)])
    for step in range(3):
        merged = add_events(merged, [event(10_000 + step)])
    assert len(merged) == MAX_EVENTS and [e["message"] for e in merged].count(OMITTED) == 1
    assert merged[-1]["message"] == "line 10002" and merged[0]["message"] == "line 0"


def test_a_list_that_already_has_a_marker_and_is_under_the_limit_is_kept_as_is():
    stored = [
        event(1),
        {"level": "warn", "step": 0, "total": 0, "name": "", "message": OMITTED},
        event(2),
    ]
    merged = add_events(stored, [event(3)])
    assert [e["message"] for e in merged] == ["line 1", OMITTED, "line 2", "line 3"]


def test_a_cut_never_repeats_a_line_even_if_a_stale_marker_is_in_the_list():
    stored = [event(n) for n in range(MAX_EVENTS)]
    stored[10] = {"level": "warn", "step": 0, "total": 0, "name": "", "message": OMITTED}
    merged = add_events(stored, [event(n) for n in range(1000, 1005)])
    messages = [e["message"] for e in merged]
    assert len(set(messages)) == len(messages) and messages.count(OMITTED) == 1


def test_long_messages_are_cut():
    assert len(add_events([], [event(1, "x" * (MAX_MESSAGE + 100))])[0]["message"]) == MAX_MESSAGE


def test_appended_logs_are_stored_with_the_job(sessions):
    target(sessions)
    job_id, _ = start(sessions)
    with sessions() as session:
        append_logs(session, job_id, [event(1)])
        append_logs(session, job_id, [event(2)])
    assert [e["message"] for e in json.loads(job_row(sessions, job_id).logs_json)] == [
        "line 1",
        "line 2",
    ]


def test_job_json_has_no_internal_columns(sessions):
    target(sessions)
    job_id, _ = start(sessions)
    shown = job_json(job_row(sessions, job_id))
    assert set(shown) == {
        "id",
        "request_id",
        "action",
        "set_name",
        "image_tag",
        "status",
        "stage",
        "logs",
        "result",
        "created_at",
        "finished_at",
    }
    assert ACTIVE == ("queued", "running")


# --- 삭제 작업 ----------------------------------------------------------------------------
def test_a_destroy_job_is_claimed_for_a_deployed_app_and_remembers_the_version_it_removes(sessions):
    deployed_target(sessions)
    job_id, created = start_destroy(sessions)
    row, held = job_row(sessions, job_id), deployment(sessions)
    assert created and (row.action, row.status, row.image_tag) == (
        "destroy",
        "queued",
        "abc1234def56",
    )
    assert (held.active_job_id, held.lease_until) == (job_id, NOW + LEASE_SECONDS)


def test_nothing_can_be_destroyed_before_the_first_deployment_or_without_a_target(sessions):
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        claim(session, "p1", uuid.uuid4(), "runtime", action="destroy")
    assert caught.value.code == "target_required"
    target(sessions)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        claim(session, "p1", uuid.uuid4(), "runtime", action="destroy")
    assert (caught.value.code, caught.value.status) == ("not_deployed", 409)
    with sessions() as session:
        assert (
            session.scalar(select(DeployJob.id)) is None
            and session.get(Deployment, "p1").active_job_id is None
        )


def test_a_destroy_waits_for_a_running_job_and_a_deploy_waits_for_a_running_destroy(sessions):
    deployed_target(sessions)
    running, _ = start(sessions)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        claim(session, "p1", uuid.uuid4(), "runtime", action="destroy", now=NOW + 5)
    assert (
        caught.value.code == "deploy_job_running" and deployment(sessions).active_job_id == running
    )
    finish_ok(sessions, running)
    destroying, _ = start_destroy(sessions, now=NOW + 10)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        claim(session, "p1", uuid.uuid4(), "runtime", now=NOW + 11)
    assert (
        caught.value.code == "deploy_job_running"
        and deployment(sessions).active_job_id == destroying
    )


def test_the_same_request_id_returns_the_same_destroy_but_never_a_different_action(sessions):
    deployed_target(sessions)
    request = uuid.uuid4()
    first, created = start_destroy(sessions, request=request)
    second, created_again = start_destroy(sessions, request=request, now=NOW + 5)
    assert (first == second, created, created_again) == (True, True, False)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        claim(session, "p1", request, "runtime")  # 같은 요청 ID로 배포를 시도
    assert caught.value.code == "request_conflict"


def test_a_deploy_request_id_cannot_be_reused_for_a_destroy(sessions):
    target(sessions)
    request = uuid.uuid4()
    start(sessions, request=request)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        claim(session, "p1", request, "runtime", action="destroy")
    assert caught.value.code == "request_conflict"


def test_an_unknown_action_is_a_programming_error(sessions):
    target(sessions)
    with sessions() as session, pytest.raises(ValueError):
        claim(session, "p1", uuid.uuid4(), "runtime", action="arbitrary_command")


def finish_destroyed(sessions, job_id):
    with sessions() as session:
        return finish(
            session,
            job_id,
            ok=True,
            result={"ok": True},
            deployed={"image_tag": "", "url": "", "app_name": ""},
            now=NOW + 20,
        )


def test_a_successful_destroy_returns_the_target_to_not_deployed_and_frees_it_for_a_new_start(
    sessions,
):
    environment_id = deployed_target(sessions)
    job_id, _ = start_destroy(sessions)
    assert finish_destroyed(sessions, job_id)
    held, row = deployment(sessions), job_row(sessions, job_id)
    assert (held.image_tag, held.url, held.app_name, held.active_job_id, held.lease_until) == (
        "",
        "",
        "",
        None,
        0,
    )
    assert held.aws_environment_id == environment_id and row.status == "succeeded"
    other = add_environment(sessions, number=4242)
    with sessions() as session:  # 지운 뒤에는 환경을 다시 고를 수 있다
        assert (
            select_target(
                session, "p1", session.get(AwsEnvironment, other), "aws-always-on"
            ).aws_environment_id
            == other
        )
    assert start(sessions, now=NOW + 30)[1]  # 다시 배포할 수 있다


def test_a_failed_destroy_keeps_what_is_running(sessions):
    deployed_target(sessions)
    job_id, _ = start_destroy(sessions)
    with sessions() as session:
        assert finish(
            session,
            job_id,
            ok=False,
            result={"ok": False, "error": {"code": "destroy_failed"}},
            stage="destroy",
            now=NOW + 20,
        )
    held, row = deployment(sessions), job_row(sessions, job_id)
    assert (held.image_tag, held.url, held.app_name, held.active_job_id) == (
        "abc1234def56",
        "https://todo.test.aws.anyship.cloud",
        "todo",
        None,
    )
    assert (row.status, row.stage) == ("failed", "destroy")


def test_a_destroy_cannot_be_started_twice_for_the_same_deployment(sessions):
    deployed_target(sessions)
    first, _ = start_destroy(sessions)
    finish_destroyed(sessions, first)
    with sessions() as session, pytest.raises(DeploymentError) as caught:
        claim(session, "p1", uuid.uuid4(), "runtime", action="destroy", now=NOW + 40)
    assert caught.value.code == "not_deployed"


def test_an_expired_destroy_lease_is_taken_over_like_any_other(sessions):
    deployed_target(sessions)
    dead, _ = start_destroy(sessions)
    fresh, created = start_destroy(sessions, now=NOW + LEASE_SECONDS)
    assert created and fresh != dead and job_row(sessions, dead).status == "interrupted"

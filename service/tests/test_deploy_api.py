"""실제 배포 API: 로그인, 프로젝트, 환경은 진짜 코드를 쓰고, 소스, 배포자, AWS 접근, 스레드만 가짜로 끼운다."""

import json
import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from anyship_adapters import AdapterError, DeployResult
from app.app import create_app
from app.config import Settings
from app.db import AwsEnvironment, Base, DeployJob, Deployment, User, database
from app.deploy_runner import DeployRunner
from app.source import SourceError
from tests.test_aws_onboarding import SERVICE_ROLE, TEMPLATE, authenticate
from tests.test_deploy_runner import (
    BUCKET,
    SHA,
    URL,
    USER_SECRET,
    DeferredExecutor,
    FakeAccess,
    FakeDeployer,
    FakeSource,
    SyncExecutor,
)
from tests.test_web import ORIGIN, FakeGitHub, add

ROLE = "arn:aws:iam::223455088214:role/deploy-service-role"


def build_web(
    database_url, tmp_path, *, executor=None, source=None, deployer=None, with_runner=True
):
    settings = Settings(
        database_url=database_url,
        token_key=Fernet.generate_key().decode(),
        github_client_id="fixture",
        github_client_secret="fixture",
        github_app_slug="fixture",
        aws_template_url=TEMPLATE,
        aws_service_role_arn=SERVICE_ROLE,
        aws_regions=("ap-northeast-2",),
    )
    engine, sessions = database(database_url)
    runner = (
        DeployRunner(
            settings,
            sessions,
            source=source or FakeSource(),
            deployer=deployer or FakeDeployer(),
            access=FakeAccess(),
            executor=executor or SyncExecutor(),
            lock_dir=tmp_path / "lock",
        )
        if with_runner
        else None
    )
    app = create_app(settings, FakeGitHub(), deploy_runner=runner)
    Base.metadata.create_all(app.state.engine)
    return app, runner, sessions


@pytest.fixture
def web(database_url, tmp_path):
    app, runner, sessions = build_web(database_url, tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        headers = authenticate(client)
        project = add(client, headers).json()
        yield SimpleNamespace(
            app=app,
            client=client,
            headers=headers,
            project=project,
            runner=runner,
            sessions=sessions,
            endpoint=f"/api/projects/{project['id']}/deployment",
        )


def make_environment(web, **override):
    workspace = web.client.get("/api/me").json()["workspace"]["id"]
    with web.app.state.sessions() as session:
        user = session.scalar(select(User.id))
    identifier = str(uuid.uuid4())
    values = dict(
        id=identifier,
        workspace_id=workspace,
        created_by=user,
        request_id=identifier,
        name="테스트 계정",
        region="ap-northeast-2",
        external_id=identifier.replace("-", "") * 2,
        template_url="https://bucket.s3.amazonaws.com/a.yaml",
        service_role_arn=SERVICE_ROLE,
        stack_name="anyship-onboarding-x",
        role_name="deploy-service-role",
        status="CONNECTED",
        role_arn=ROLE,
        aws_account_id="223455088214",
        created_at=1,
        expires_at=2,
        state_bucket=BUCKET,
    )
    with web.app.state.sessions() as session:
        session.add(AwsEnvironment(**{**values, **override}))
        session.commit()
    return identifier


def put_target(web, environment_id, set_name="aws-always-on"):
    return web.client.put(
        web.endpoint,
        headers=web.headers,
        json={"environment_id": environment_id, "set_name": set_name},
    )


def deploy(web, request_id=None, secrets=None):
    body = {"request_id": request_id or str(uuid.uuid4())}
    if secrets is not None:
        body["secrets"] = secrets
    return web.client.post(web.endpoint + "/jobs", headers=web.headers, json=body)


def destroy(web, request_id=None, **extra):
    return web.client.post(
        web.endpoint + "/jobs",
        headers=web.headers,
        json={"request_id": request_id or str(uuid.uuid4()), "action": "destroy", **extra},
    )


def ready(web):
    environment_id = make_environment(web)
    assert put_target(web, environment_id).status_code == 200
    return environment_id


def database_text(web):
    with web.app.state.sessions() as session:
        return json.dumps(
            [
                [str(getattr(row, c.name)) for c in row.__table__.columns]
                for model in (DeployJob, Deployment, AwsEnvironment)
                for row in session.scalars(select(model))
            ],
            ensure_ascii=False,
        )


# --- 조회와 대상 선택 ----------------------------------------------------------------------
def test_the_overview_lists_environments_and_marks_unconnected_ones_unavailable(web):
    ready_id = make_environment(web, name="연결됨")
    pending_id = make_environment(
        web, name="대기", status="PENDING", role_arn=None, aws_account_id=None
    )
    failed_id = make_environment(
        web, name="실패", status="FAILED", role_arn=ROLE.replace("role/", "role/failed-")
    )
    body = web.client.get(web.endpoint).json()
    assert body["target"] is None and body["sets"] == ["aws-always-on"]
    available = {item["id"]: item["available"] for item in body["environments"]}
    assert available == {
        ready_id: True,
        pending_id: False,
        failed_id: False,
    }  # 역할이 있어도 연결이 확인되어야 한다


def test_a_connected_environment_can_be_selected_and_shows_up_in_the_overview(web):
    environment_id = make_environment(web)
    saved = put_target(web, environment_id)
    assert saved.status_code == 200
    target = web.client.get(web.endpoint).json()["target"]
    assert target == saved.json() and target["environment_id"] == environment_id
    assert (target["set_name"], target["deployed"], target["busy"], target["url"]) == (
        "aws-always-on",
        False,
        False,
        "",
    )


def test_unknown_unconnected_or_unsupported_selections_are_refused(web):
    pending = make_environment(web, status="PENDING", role_arn=None, aws_account_id=None)
    assert put_target(web, str(uuid.uuid4())).status_code == 404
    refused = put_target(web, pending)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (
        409,
        "environment_not_connected",
    )
    good = make_environment(web, role_arn=ROLE.replace("role/", "role/other-"))
    refused = put_target(web, good, "aws-serverless")
    assert (refused.status_code, refused.json()["detail"]["code"]) == (422, "set_not_supported")


def test_another_users_environment_cannot_be_selected(web, database_url):
    with web.app.state.sessions() as session:
        other = User(id="stranger", github_id=99, login="x", name="x")
        session.add(other)
        session.commit()
    foreign = make_environment(web, created_by="stranger")
    assert put_target(web, foreign).status_code == 404


def test_changes_need_the_csrf_token_and_a_login(web):
    environment_id = make_environment(web)
    body = {"environment_id": environment_id, "set_name": "aws-always-on"}
    assert web.client.put(web.endpoint, headers={"Origin": ORIGIN}, json=body).status_code == 403
    assert (
        web.client.post(
            web.endpoint + "/jobs",
            headers={"Origin": ORIGIN},
            json={"request_id": str(uuid.uuid4())},
        ).status_code
        == 403
    )
    anonymous = TestClient(
        web.app, base_url=ORIGIN
    )  # with 없이: 앱 시작 절차(실행기 잠금)를 다시 돌리지 않는다
    assert anonymous.get(web.endpoint).status_code == 401


def test_the_runner_is_started_with_the_app_and_stopped_with_it(database_url, tmp_path):
    app, runner, _ = build_web(database_url, tmp_path)
    assert runner.executor is None
    with TestClient(app, base_url=ORIGIN):
        assert runner.executor is not None and runner.lock_file is not None
    assert runner.executor is None and runner.lock_file is None


def test_a_project_of_another_user_is_not_found(web, database_url):
    with web.app.state.sessions() as session:
        project = session.scalar(select(__import__("app.db", fromlist=["Project"]).Project))
        session.add(User(id="stranger", github_id=77, login="y", name="y"))
        session.flush()
        project.created_by = "stranger"
        session.commit()
    assert web.client.get(web.endpoint).status_code == 404


def test_an_unknown_project_is_not_found(web):
    assert web.client.get("/api/projects/nope/deployment").status_code == 404


def test_without_a_runner_every_call_says_the_feature_is_off(database_url, tmp_path):
    app, _, _ = build_web(database_url, tmp_path, with_runner=False)
    with TestClient(app, base_url=ORIGIN) as client:
        headers = authenticate(client)
        project = add(client, headers).json()
        response = client.get(f"/api/projects/{project['id']}/deployment")
    assert (response.status_code, response.json()["detail"]["code"]) == (503, "deploy_unavailable")


# --- 배포 ----------------------------------------------------------------------------------
def test_a_deploy_request_runs_and_the_job_can_be_read_back(web):
    ready(web)
    response = deploy(web, secrets={"API_KEY": USER_SECRET})
    assert response.status_code == 202
    job = web.client.get(f"{web.endpoint}/jobs/{response.json()['id']}").json()
    assert (job["status"], job["image_tag"], job["stage"]) == ("succeeded", SHA, "")
    assert job["result"]["ok"] is True and [e["message"] for e in job["logs"]] == ["빌드"]
    target = web.client.get(web.endpoint).json()["target"]
    assert (target["deployed"], target["image_tag"], target["url"], target["app_name"]) == (
        True,
        SHA,
        URL,
        "todo",
    )
    assert [item["id"] for item in web.client.get(web.endpoint + "/jobs").json()] == [job["id"]]


def test_the_same_request_id_returns_the_same_job_with_200(web):
    ready(web)
    request_id = str(uuid.uuid4())
    first, second = deploy(web, request_id), deploy(web, request_id)
    assert (first.status_code, second.status_code, first.json()["id"] == second.json()["id"]) == (
        202,
        200,
        True,
    )


def test_user_secrets_reach_the_deployer_but_nothing_else(web):
    ready(web)
    response = deploy(web, secrets={"API_KEY": USER_SECRET})
    (call,) = web.runner.deployer.calls
    assert call["secrets"] == {"API_KEY": USER_SECRET}
    assert (
        USER_SECRET not in response.text
        and USER_SECRET not in web.client.get(web.endpoint + "/jobs").text
    )
    assert USER_SECRET not in database_text(web)


def test_a_failed_deploy_shows_its_stage_and_error(web):
    ready(web)
    web.runner.deployer.behaviour = lambda log, secrets: DeployResult(
        ok=False,
        error=AdapterError(code="docker_build_failed", message="빌드 실패"),
        details={"stage": "build"},
    )
    job = web.client.get(f"{web.endpoint}/jobs/{deploy(web).json()['id']}").json()
    assert (job["status"], job["stage"], job["result"]["error"]["code"]) == (
        "failed",
        "build",
        "docker_build_failed",
    )
    assert web.client.get(web.endpoint).json()["target"]["deployed"] is False


def test_problems_found_before_starting_are_returned_at_once_without_a_job(web):
    environment_id = ready(web)
    web.runner.source.error = SourceError(
        "source_not_found", "이 저장소의 소스 폴더를 찾을 수 없습니다."
    )
    refused = deploy(web)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "source_not_found")
    web.runner.source.error = None
    with web.app.state.sessions() as session:
        session.get(AwsEnvironment, environment_id).status = "FAILED"
        session.commit()
    assert deploy(web).json()["detail"]["code"] == "environment_not_connected"
    assert web.client.get(web.endpoint + "/jobs").json() == []


def test_a_deploy_needs_a_selected_environment(web):
    refused = deploy(web)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "target_required")


def test_a_second_deploy_waits_for_the_first(database_url, tmp_path):
    executor = DeferredExecutor()
    app, runner, _ = build_web(database_url, tmp_path, executor=executor)
    with TestClient(app, base_url=ORIGIN) as client:
        headers = authenticate(client)
        project = add(client, headers).json()
        web = SimpleNamespace(
            app=app,
            client=client,
            headers=headers,
            endpoint=f"/api/projects/{project['id']}/deployment",
        )
        environment_id = make_environment(web)
        put_target(web, environment_id)
        first = deploy(web)
        assert first.json()["status"] == "queued"
        refused = deploy(web)
        assert (refused.status_code, refused.json()["detail"]["code"]) == (
            409,
            "deploy_job_running",
        )
        busy = client.get(web.endpoint).json()["target"]
        assert busy["busy"] is True and busy["active_job_id"] == first.json()["id"]
        executor.run_all()
        assert deploy(web).status_code == 202


def test_a_job_of_another_project_is_not_visible(web):
    ready(web)
    job = deploy(web).json()
    other = add(web.client, web.headers, repository_id=1002).json()
    assert (
        web.client.get(f"/api/projects/{other['id']}/deployment/jobs/{job['id']}").status_code
        == 404
    )
    assert web.client.get(f"{web.endpoint}/jobs/{uuid.uuid4()}").status_code == 404


# --- 입력 검증 ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "secrets",
    [
        {"lower": USER_SECRET},
        {"1BAD": USER_SECRET},
        {"A B": USER_SECRET},
        {"API_KEY": "x" * 4097},
        {f"K{n}": USER_SECRET for n in range(51)},
        {"API_KEY": 123},
        ["not", "a", "mapping"],
    ],
)
def test_invalid_secrets_are_refused_without_echoing_any_value(web, secrets):
    ready(web)
    refused = deploy(web, secrets=secrets)
    assert (
        refused.status_code == 422
        and USER_SECRET not in refused.text
        and "x" * 100 not in refused.text
    )
    assert web.client.get(web.endpoint + "/jobs").json() == []


def test_a_body_with_unknown_fields_or_a_bad_request_id_is_refused_without_echo(web):
    ready(web)
    for body in (
        {"request_id": str(uuid.uuid4()), "image_tag": USER_SECRET},
        {"request_id": USER_SECRET},
    ):
        refused = web.client.post(web.endpoint + "/jobs", headers=web.headers, json=body)
        assert refused.status_code == 422 and USER_SECRET not in refused.text


def test_query_errors_on_the_job_list_keep_the_normal_validation_message(web):
    ready(web)
    refused = web.client.get(web.endpoint + "/jobs", params={"limit": 0})
    assert refused.status_code == 422 and "비밀" not in refused.text


# --- 삭제 ----------------------------------------------------------------------------------
def test_deleting_a_project_removes_its_deployment_records(web):
    ready(web)
    deploy(web)
    assert (
        web.client.delete(f"/api/projects/{web.project['id']}", headers=web.headers).status_code
        == 204
    )
    with web.app.state.sessions() as session:
        assert (
            session.scalar(select(Deployment.project_id)) is None
            and session.scalar(select(DeployJob.id)) is None
        )


def test_a_project_with_a_running_job_cannot_be_deleted(database_url, tmp_path):
    executor = DeferredExecutor()
    app, runner, _ = build_web(database_url, tmp_path, executor=executor)
    with TestClient(app, base_url=ORIGIN) as client:
        headers = authenticate(client)
        project = add(client, headers).json()
        web = SimpleNamespace(
            app=app,
            client=client,
            headers=headers,
            endpoint=f"/api/projects/{project['id']}/deployment",
        )
        put_target(web, make_environment(web))
        deploy(web)
        refused = client.delete(f"/api/projects/{project['id']}", headers=headers)
        assert (refused.status_code, refused.json()["detail"]["code"]) == (
            409,
            "deploy_job_running",
        )
        executor.run_all()
        assert client.delete(f"/api/projects/{project['id']}", headers=headers).status_code == 204


def test_an_environment_used_as_a_target_cannot_be_deleted_until_the_project_is_gone(web):
    environment_id = ready(web)
    refused = web.client.delete(f"/api/aws/environments/{environment_id}", headers=web.headers)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "environment_in_use")
    web.client.delete(f"/api/projects/{web.project['id']}", headers=web.headers)
    assert (
        web.client.delete(
            f"/api/aws/environments/{environment_id}", headers=web.headers
        ).status_code
        == 204
    )


# --- 배포 삭제 -----------------------------------------------------------------------------
def test_a_deployed_app_can_be_removed_and_the_target_goes_back_to_not_deployed(web):
    ready(web)
    deploy(web)
    assert web.client.get(web.endpoint).json()["target"]["deployed"] is True
    response = destroy(web)
    assert response.status_code == 202
    job = web.client.get(f"{web.endpoint}/jobs/{response.json()['id']}").json()
    assert (job["action"], job["status"], job["stage"], job["image_tag"]) == (
        "destroy",
        "succeeded",
        "",
        SHA,
    )
    target = web.client.get(web.endpoint).json()["target"]
    assert (
        target["deployed"],
        target["image_tag"],
        target["url"],
        target["app_name"],
        target["busy"],
    ) == (False, "", "", "", False)
    assert [item["action"] for item in web.client.get(web.endpoint + "/jobs").json()] == [
        "destroy",
        "deploy",
    ]
    (call,) = web.runner.deployer.destroy_calls
    assert (call["app"], call["set_name"]) == ("todo", "aws-always-on")


def test_after_a_removal_the_environment_can_be_changed_and_the_app_deployed_again(web):
    ready(web)
    deploy(web)
    destroy(web)
    other = make_environment(web, role_arn=ROLE.replace("role/", "role/second-"))
    assert put_target(web, other).status_code == 200
    assert (
        deploy(web).status_code == 202
        and web.client.get(web.endpoint).json()["target"]["deployed"] is True
    )


def test_a_removal_is_refused_when_nothing_was_deployed_or_nothing_is_selected(web):
    refused = destroy(web)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "target_required")
    ready(web)
    refused = destroy(web)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "not_deployed")
    assert (
        web.client.get(web.endpoint + "/jobs").json() == []
        and web.runner.deployer.destroy_calls == []
    )


def test_a_removal_is_refused_for_an_environment_that_is_not_connected(web):
    environment_id = ready(web)
    deploy(web)
    with web.app.state.sessions() as session:
        session.get(AwsEnvironment, environment_id).status = "FAILED"
        session.commit()
    refused = destroy(web)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (
        409,
        "environment_not_connected",
    )
    assert web.runner.deployer.destroy_calls == []


def test_the_same_removal_request_returns_the_same_job_and_runs_once(web):
    ready(web)
    deploy(web)
    request_id = str(uuid.uuid4())
    first, second = destroy(web, request_id), destroy(web, request_id)
    assert (first.status_code, second.status_code, first.json()["id"] == second.json()["id"]) == (
        202,
        200,
        True,
    )
    assert len(web.runner.deployer.destroy_calls) == 1


def test_a_request_id_cannot_switch_between_deploying_and_removing(web):
    ready(web)
    request_id = str(uuid.uuid4())
    deploy(web, request_id)
    refused = destroy(web, request_id)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "request_conflict")


def test_a_removal_takes_no_secrets_and_never_echoes_them(web):
    ready(web)
    deploy(web)
    refused = destroy(web, secrets={"API_KEY": USER_SECRET})
    assert refused.status_code == 422 and USER_SECRET not in refused.text
    assert web.runner.deployer.destroy_calls == []
    assert (
        destroy(web, secrets={}).status_code == 202
    )  # 비어 있는 비밀은 괜찮다(화면이 항상 보낸다)


def test_an_unknown_action_is_refused_without_echoing_it(web):
    ready(web)
    refused = web.client.post(
        web.endpoint + "/jobs",
        headers=web.headers,
        json={"request_id": str(uuid.uuid4()), "action": USER_SECRET},
    )
    assert refused.status_code == 422 and USER_SECRET not in refused.text


def test_a_removal_waits_for_a_running_deploy(database_url, tmp_path):
    executor = DeferredExecutor()
    app, runner, _ = build_web(database_url, tmp_path, executor=executor)
    with TestClient(app, base_url=ORIGIN) as client:
        headers = authenticate(client)
        project = add(client, headers).json()
        web = SimpleNamespace(
            app=app,
            client=client,
            headers=headers,
            endpoint=f"/api/projects/{project['id']}/deployment",
        )
        put_target(web, make_environment(web))
        deploy(web)
        executor.run_all()
        deploy(web)  # 두 번째 배포가 대기 중
        refused = destroy(web)
        assert (refused.status_code, refused.json()["detail"]["code"]) == (
            409,
            "deploy_job_running",
        )
        executor.run_all()
        assert destroy(web).status_code == 202


def test_removal_needs_the_csrf_token(web):
    ready(web)
    deploy(web)
    refused = web.client.post(
        web.endpoint + "/jobs",
        headers={"Origin": ORIGIN},
        json={"request_id": str(uuid.uuid4()), "action": "destroy"},
    )
    assert refused.status_code == 403 and web.runner.deployer.destroy_calls == []


def test_a_failed_removal_keeps_the_deployment_and_shows_why(web):
    from anyship_adapters import DestroyResult

    ready(web)
    deploy(web)
    web.runner.deployer.destroy_behaviour = lambda log: DestroyResult(
        ok=False,
        error=AdapterError(
            code="destroy_failed",
            message="컨테이너를 지우지 못했습니다.",
            hint="서버를 확인해 주세요.",
        ),
    )
    job = web.client.get(f"{web.endpoint}/jobs/{destroy(web).json()['id']}").json()
    assert (job["status"], job["stage"], job["result"]["error"]["code"]) == (
        "failed",
        "destroy",
        "destroy_failed",
    )
    assert web.client.get(web.endpoint).json()["target"]["deployed"] is True


# --- GitHub 토큰 ---------------------------------------------------------------------------
def test_the_users_login_token_reaches_the_source_fetch_and_nothing_else(web):
    ready(web)
    response = deploy(web, secrets={"API_KEY": USER_SECRET})
    ((full_name, branch, token),) = web.runner.source.calls
    assert (full_name, branch) == (
        "demo-workspace/todo-api",
        "main",
    )  # 연결한 프로젝트의 저장소와 브랜치
    assert isinstance(token, str) and token  # 로그인 세션에 암호화해 둔 사용자 토큰을 풀어서 넘겼다
    assert token not in response.text and token not in web.client.get(web.endpoint + "/jobs").text
    assert token not in database_text(web) and token not in repr(web.runner.deployer.calls)


def test_a_session_whose_token_cannot_be_read_is_asked_to_log_in_again_and_fetches_nothing(web):
    ready(web)
    with web.app.state.sessions() as session:
        from app.db import LoginSession

        for row in session.query(LoginSession).all():
            row.token_cipher = "not-a-valid-token"
        session.commit()
    refused = deploy(web)
    assert refused.status_code == 401 and web.runner.source.calls == []


def test_a_removal_does_not_need_or_use_the_github_token(web):
    ready(web)
    deploy(web)
    fetches = len(web.runner.source.calls)
    assert destroy(web).status_code == 202 and len(web.runner.source.calls) == fetches


def test_a_source_that_cannot_be_fetched_is_reported_at_once_without_the_token(web):
    ready(web)
    web.runner.source.error = SourceError(
        "source_fetch_failed", "GitHub에서 소스를 받지 못했습니다."
    )
    refused = deploy(web)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "source_fetch_failed")
    token = web.runner.source.calls[0][2]
    assert token not in refused.text and web.client.get(web.endpoint + "/jobs").json() == []

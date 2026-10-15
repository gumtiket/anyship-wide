import pytest

from anyship_adapters import (
    DEPLOY_STEPS,
    MASK,
    AwsEnvironment,
    LogEvent,
    MockAdapter,
    OnpremEnvironment,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/deploy-service-role"
TAG = "3f2a9c1"
SECRET = "s3cr3t-value-123"


@pytest.fixture
def onprem():
    return OnpremEnvironment(env_id="demo", host="3.38.88.141")


@pytest.fixture
def aws():
    return AwsEnvironment(env_id="demo", role_arn=ROLE_ARN, external_id="a" * 32)


def spec(**override):
    return {
        "app": "todo-a1b2",
        "backing_services": [{"type": "postgres", "bind_as": "DATABASE_URL"}],
        **override,
    }


class Collector:
    def __init__(self):
        self.events: list[LogEvent] = []

    def __call__(self, event):
        self.events.append(event)


def deploy(adapter, env, log=None, **kw):
    args = dict(spec=spec(), image_tag=TAG, secrets={}, set_name="onprem")
    args.update(kw)
    return adapter.deploy(
        env,
        args["spec"],
        args["image_tag"],
        args["secrets"],
        log or Collector(),
        set_name=args["set_name"],
    )


# --- 성공 흐름 ----------------------------------------------------------------------------
def test_check_succeeds_for_both_kinds(onprem, aws):
    adapter = MockAdapter()
    assert adapter.check(onprem, Collector()).ok
    assert adapter.check(aws, Collector()).details["account_id"] == "123456789012"


def test_deploy_reports_numbered_steps_in_order_and_returns_the_url(onprem):
    log = Collector()
    result = deploy(MockAdapter(), onprem, log)
    assert result.ok and result.url == "https://todo-a1b2.demo.onprem.anyship.cloud"
    assert result.image_tag == TAG
    assert [e.step for e in log.events] == [1, 2, 3, 4, 5]
    assert all(e.total == len(DEPLOY_STEPS) for e in log.events)
    assert [e.name for e in log.events] == list(DEPLOY_STEPS)


def test_urls_follow_the_set(aws):
    assert (
        deploy(MockAdapter(), aws, set_name="aws-always-on").url
        == "https://todo-a1b2.demo.aws.anyship.cloud"
    )
    assert deploy(MockAdapter(), aws, set_name="aws-serverless").url.endswith(
        ".lambda-url.ap-northeast-2.on.aws/"
    )


def test_status_follows_the_lifecycle(onprem):
    adapter = MockAdapter()
    assert adapter.status(onprem, "todo-a1b2").state == "not_deployed"
    deploy(adapter, onprem)
    running = adapter.status(onprem, "todo-a1b2")
    assert running.state == "running" and running.image_tag == TAG
    assert adapter.destroy(onprem, "todo-a1b2", Collector()).ok
    assert adapter.status(onprem, "todo-a1b2").state == "not_deployed"


def test_rollback_runs_the_previous_tag_and_keeps_the_url(onprem):
    adapter = MockAdapter()
    first = deploy(adapter, onprem, image_tag="aaaaaaa")
    deploy(adapter, onprem, image_tag="bbbbbbb")
    result = adapter.rollback(onprem, "todo-a1b2", "aaaaaaa", Collector())
    assert result.ok and result.image_tag == "aaaaaaa" and result.url == first.url
    assert adapter.status(onprem, "todo-a1b2").image_tag == "aaaaaaa"


def test_destroy_is_idempotent(onprem):
    assert MockAdapter().destroy(onprem, "never-deployed", Collector()).ok


def test_apps_are_kept_per_environment(onprem):
    other = OnpremEnvironment(env_id="other", host="3.38.88.142")
    adapter = MockAdapter()
    deploy(adapter, onprem)
    assert adapter.status(other, "todo-a1b2").state == "not_deployed"


# --- 실패 시나리오 ----------------------------------------------------------------------------
def test_check_fails_with_an_error_that_matches_the_environment(onprem, aws):
    adapter = MockAdapter("check_fails")
    assert adapter.check(onprem, Collector()).error.code == "ssh_unreachable"
    assert adapter.check(aws, Collector()).error.code == "assume_role_denied"


def test_deploy_fails_at_the_start_step_and_leaves_nothing_behind(onprem):
    adapter = MockAdapter("deploy_fails")
    log = Collector()
    result = deploy(adapter, onprem, log)
    assert (
        not result.ok and result.error.code == "container_start_failed" and result.error.retryable
    )
    assert log.events[-1].level == "error" and log.events[-1].step == 4
    assert adapter.status(onprem, "todo-a1b2").state == "not_deployed"


def test_unhealthy_fails_at_the_last_step(onprem):
    result = deploy(MockAdapter("unhealthy"), onprem)
    assert result.error.code == "healthcheck_failed"


def test_unknown_scenario_is_rejected():
    with pytest.raises(ValueError):
        MockAdapter("nope")


# --- 입력 검증(예외가 아니라 오류로 답한다) -------------------------------------------
@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"image_tag": "latest"}, "invalid_image_tag"),
        ({"image_tag": "3f2a9c1; rm"}, "invalid_image_tag"),
        ({"spec": {"app": "Bad Name"}}, "invalid_spec"),
        ({"spec": {}}, "invalid_spec"),
        (
            {"set_name": "aws-always-on"},
            "set_not_supported",
        ),  # 온프레미스 환경에 AWS 세트를 쓴 경우
        (
            {"spec": spec(backing_services=[{"type": "object_storage", "bind_as": "STORAGE_URL"}])},
            "unsupported_backing_service",
        ),
    ],
)
def test_bad_deploy_requests_return_an_error(onprem, kwargs, code):
    log = Collector()
    result = deploy(MockAdapter(), onprem, log, **kwargs)
    assert not result.ok and result.error.code == code
    assert log.events == []  # 아무것도 실행되지 않았다


def test_aws_env_cannot_use_the_onprem_set(aws):
    assert deploy(MockAdapter(), aws, set_name="onprem").error.code == "set_not_supported"


def test_rollback_needs_a_deployed_app_and_a_valid_tag(onprem):
    adapter = MockAdapter()
    assert adapter.rollback(onprem, "todo-a1b2", TAG, Collector()).error.code == "app_not_found"
    deploy(adapter, onprem)
    assert (
        adapter.rollback(onprem, "todo-a1b2", "nope", Collector()).error.code == "invalid_image_tag"
    )


# --- 비밀 값 -----------------------------------------------------------------------------------------
def test_secret_values_never_reach_logs_or_results_only_their_names_do(onprem):
    log = Collector()
    result = deploy(MockAdapter(), onprem, log, secrets={"API_KEY": SECRET})
    everything = result.model_dump_json() + "".join(e.model_dump_json() for e in log.events)
    assert SECRET not in everything
    assert result.details["secrets_stored"] == ["API_KEY"]


def test_even_a_careless_adapter_cannot_leak_a_secret_through_the_wrapped_log(onprem):
    class Careless(MockAdapter):
        def _run_deploy(self, env, app, image_tag, set_name, log, secrets):
            log(LogEvent(message=f"debug: {secrets}", data={"raw": SECRET}))
            return super()._run_deploy(env, app, image_tag, set_name, log, secrets)

    log = Collector()
    deploy(Careless(), onprem, log, secrets={"API_KEY": SECRET})
    assert SECRET not in "".join(e.model_dump_json() for e in log.events)
    assert MASK in log.events[0].message


# --- 진행 속도 --------------------------------------------------------------------------------------------
def test_delay_is_applied_per_step_and_can_be_faked(onprem):
    pauses = []
    deploy(MockAdapter(delay=0.5, sleep=pauses.append), onprem)
    assert pauses == [0.5] * len(DEPLOY_STEPS)

from pathlib import Path

import pytest
from fakes import Log
from specs import GENERATED, make

from anyship_adapters import (
    AdapterError,
    AwsEnvironment,
    CheckResult,
    DeployResult,
    DestroyResult,
    LogEvent,
    OnpremEnvironment,
)
from anyship_adapters.deployer import Deployer
from anyship_adapters.dns import DnsError
from anyship_adapters.foundation import FoundationSettings
from anyship_adapters.image_builder import BuildError, BuiltImage
from anyship_adapters.terraform_runner import TerraformError

SHA, SRC = "abc1234", Path("/srv/source")
URL = "https://todo.test.aws.anyship.cloud"
BUCKET = "anyship-tfstate-223455088214-ap-northeast-2-2b9b6060"
FILLED = dict(
    host="43.201.158.8",
    db_address="anyship-test-db.abc.ap-northeast-2.rds.amazonaws.com",
    db_port=5432,
    db_secret_arn="arn:aws:secretsmanager:ap-northeast-2:223455088214:secret:rds!db-abc-Xy1",
)
SETTINGS = FoundationSettings("203.0.113.10", "ssh-ed25519 AAAA deploy", "ops@example.com")
ENV = AwsEnvironment(
    env_id="test",
    role_arn="arn:aws:iam::223455088214:role/deploy-service-role",
    external_id="ext-id-0123456789abcdef",
    state_bucket=BUCKET,
    host="1.1.1.1",
)  # 서비스에 저장된 낡은 값
ONPREM_ENV = OnpremEnvironment(env_id="demo", host="203.0.113.5")
USER_SECRET = "sk-live-0123456789abcdef"


def err(code, message="boom", **extra):
    return AdapterError(code=code, message=message, **extra)


class Parts:
    """부품들이 부른 순서를 order 하나에 모은다."""

    def __init__(self):
        self.order, self.builds, self.checks, self.deploys, self.applied = [], [], [], [], []
        self.build_error = self.runner_reads = self.check_result = self.deploy_result = (
            self.prune_error
        ) = None
        self.deploy_raises = None
        self.destroys, self.destroy_result, self.destroy_raises = [], None, None
        self.destroy_message = "앱 디렉터리를 지우는 중"
        self.check_message, self.checks_raise = "접속하는 중", None
        self.cleanups, self.cleanup_result, self.cleanup_raises = [], None, None
        self.runner_reads = ["ok"]


def make_deployer(parts, *, runner=True, kinds=("aws-always-on", "onprem"), dns=None):
    class Builder:
        def build(self, source_dir, app, sha, log, *, dockerfile="Dockerfile"):
            parts.order.append("build")
            parts.builds.append((Path(source_dir), app, sha, dockerfile))
            log(LogEvent(step=2, total=2, name="이미지 빌드", message="빌드 로그 줄"))
            if parts.build_error:
                raise parts.build_error
            return BuiltImage(image=f"{app}:{sha}", image_id="sha256:abc")

        def prune(self, app, keep):
            parts.order.append("prune")
            if parts.prune_error:
                raise parts.prune_error
            return [f"{app}:old1"]

    class Runner:
        def read_foundation(self, env, log):
            parts.order.append("read")
            log(LogEvent(step=2, total=2, name="terraform output", message="출력을 읽는 중"))
            result = parts.runner_reads.pop(0)
            if isinstance(result, Exception):
                raise result
            return env.model_copy(update=FILLED)

        def apply(self, env, variables, log):
            parts.order.append("apply")
            parts.applied.append(variables)
            log(LogEvent(step=2, total=2, name="terraform apply", message="만드는 중"))

    class Adapter:
        def check(self, env, log):
            parts.order.append("check")
            parts.checks.append(env)
            log(LogEvent(step=1, total=5, name="접속 확인", message=parts.check_message))
            if parts.checks_raise:
                raise parts.checks_raise
            return parts.check_result or CheckResult(ok=True)

        def deploy(self, env, spec, image_tag, secrets, log, *, set_name):
            parts.order.append("deploy")
            parts.deploys.append((env, image_tag, dict(secrets), set_name))
            log(
                LogEvent(
                    step=3,
                    total=8,
                    name="앱 시작",
                    message=f"시작 {USER_SECRET if secrets else ''}".strip(),
                )
            )
            if parts.deploy_raises:
                raise parts.deploy_raises
            return parts.deploy_result or DeployResult(
                ok=True, url=URL, image_tag=image_tag, details={"database": "app_todo"}
            )

        def destroy(self, env, app, log):
            parts.order.append("destroy")
            parts.destroys.append((env, app))
            log(LogEvent(step=2, total=2, name="파일 제거", message=parts.destroy_message))
            if parts.destroy_raises:
                raise parts.destroy_raises
            return parts.destroy_result or DestroyResult(ok=True)

    class OnpremLike(Adapter):
        """환경 단위 정리(DNS)를 가진 어댑터. AWS 어댑터는 이 함수가 없다."""

        def remove_environment_dns(self, env, log):
            parts.order.append("remove_environment")
            parts.cleanups.append(env)
            log(LogEvent(step=1, total=1, name="DNS 제거", message="환경의 DNS 레코드를 지우는 중"))
            if parts.cleanup_raises:
                raise parts.cleanup_raises
            return parts.cleanup_result or DestroyResult(ok=True)

    adapters = {name: (OnpremLike() if name == "onprem" else Adapter()) for name in kinds}
    kwargs = dict(runner=Runner(), foundation=SETTINGS) if runner else {}
    return Deployer(adapters, Builder(), dns=dns, **kwargs)


def run(
    parts=None,
    env=ENV,
    spec=None,
    sha=SHA,
    secrets=None,
    set_name="aws-always-on",
    **deployer_options,
):
    parts = parts or Parts()
    log = Log()
    result = make_deployer(parts, **deployer_options).deploy(
        env, spec or GENERATED, SRC, sha, secrets or {}, log, set_name=set_name
    )
    return result, parts, log


MISSING = TerraformError(err("foundation_missing", "공용 기반이 아직 만들어지지 않았습니다."))


# --- 정상 경로 ---------------------------------------------------------------------------------
def test_an_aws_deploy_runs_every_stage_in_order_and_hands_back_the_foundation_values():
    result, parts, _ = run()
    assert result.ok and result.url == URL and result.image_tag == SHA
    assert parts.order == ["build", "read", "check", "deploy", "prune"]
    assert result.details["foundation"] == FILLED and result.details["foundation_created"] is False
    assert result.details["image"] == f"todo:{SHA}" and result.details["pruned"] == ["todo:old1"]
    assert result.details["database"] == "app_todo"  # 어댑터의 details는 그대로 남는다


def test_the_adapter_gets_the_foundation_read_now_not_the_stale_values_the_service_stored():
    _, parts, _ = run()
    assert parts.checks[0].host == "43.201.158.8" and parts.deploys[0][0].host == "43.201.158.8"


def test_a_missing_foundation_is_created_during_the_deploy():
    parts = Parts()
    parts.runner_reads = [MISSING, "ok"]
    result, parts, log = run(parts)
    assert result.ok and result.details["foundation_created"] is True
    assert parts.order == ["build", "read", "apply", "read", "check", "deploy", "prune"]
    assert parts.applied[0]["env_id"] == "test" and parts.applied[0]["account_id"] == "223455088214"
    assert any("새로 만듭니다" in e.message and e.name == "공용 기반 확인" for e in log.events)


def test_the_stages_are_numbered_continuously_and_inner_numbers_stay_in_the_message():
    _, _, log = run()
    steps = [(e.step, e.total, e.name) for e in log.events if e.step]
    assert {total for _, total, _ in steps} == {5}
    names = list(dict.fromkeys(name for _, _, name in steps))
    assert names == ["이미지 빌드", "공용 기반 확인", "연결 확인", "배포", "정리"]
    assert [step for step, _, _ in steps] == sorted(
        step for step, _, _ in steps
    )  # 거꾸로 가지 않는다
    assert any(e.name == "배포" and e.message.startswith("[3/8] ") for e in log.events)
    assert any(e.name == "공용 기반 확인" and e.message.startswith("[2/2] ") for e in log.events)


def test_an_onprem_deploy_skips_the_foundation_and_has_four_stages():
    result, parts, log = run(env=ONPREM_ENV, set_name="onprem")
    assert result.ok and parts.order == ["build", "check", "deploy", "prune"]
    assert "foundation" not in result.details and {e.total for e in log.events if e.step} == {4}


def test_without_a_terraform_runner_the_environment_is_used_as_given():
    result, parts, log = run(runner=False)
    assert result.ok and parts.order == ["build", "check", "deploy", "prune"]
    assert parts.checks[0].host == "1.1.1.1" and "foundation" not in result.details
    assert {e.total for e in log.events if e.step} == {4}


def test_the_dockerfile_path_comes_from_the_spec_and_defaults_to_dockerfile():
    _, parts, _ = run(spec=make(build={"dockerfile": "docker/Dockerfile.prod"}))
    assert parts.builds[0] == (SRC, "todo", SHA, "docker/Dockerfile.prod")
    _, parts, _ = run(spec=make())
    assert parts.builds[0][3] == "Dockerfile"


# --- 시작하기 전에 거르는 입력 오류 --------------------------------------------------------------------
NEED_SECRET = make(
    env=GENERATED["env"] + [{"name": "STRIPE_KEY", "secret": True, "generate": False}]
)


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"spec": {"port": 8080}}, "invalid_spec"),
        ({"spec": NEED_SECRET}, "missing_secret"),
        ({"sha": "xyz"}, "invalid_image_tag"),
        ({"spec": make(app="a" * 60)}, "invalid_spec"),
        ({"set_name": "aws-serverless"}, "set_not_supported"),  # 이 배포자에 없는 세트
        ({"set_name": "onprem"}, "set_not_supported"),  # aws 환경에 온프레미스 세트
        ({"env": ONPREM_ENV}, "set_not_supported"),  # 온프레미스 환경에 aws 세트
        ({"set_name": "no-such-set"}, "set_not_supported"),
    ],
)
def test_input_errors_are_found_before_anything_starts(kwargs, code):
    result, parts, log = run(**kwargs)
    assert not result.ok and result.error.code == code and result.details["stage"] == "spec"
    assert parts.order == [] and not any(
        e.step for e in log.events
    )  # 빌드도 기반 생성도 시작하지 않았다


def test_a_user_secret_that_is_provided_passes_the_check():
    result, parts, _ = run(spec=NEED_SECRET, secrets={"STRIPE_KEY": USER_SECRET})
    assert result.ok and parts.deploys[0][2] == {"STRIPE_KEY": USER_SECRET}


def test_an_invalid_sha_is_not_echoed_back_as_the_image_tag():
    result, _, _ = run(sha="../../x")
    assert result.image_tag is None


# --- 단계별 실패 --------------------------------------------------------------------------------
def build_fails(parts):
    parts.build_error = BuildError(
        err("docker_build_failed", "이미지 빌드가 실패했습니다."), tail="pip failed"
    )


def foundation_fails(parts):
    parts.runner_reads = [TerraformError(err("access_denied", "역할을 맡을 수 없습니다."))]


def check_fails(parts):
    parts.check_result = CheckResult(
        ok=False, error=err("ssh_unreachable", "서버에 접속할 수 없습니다.", retryable=True)
    )


def deploy_fails(parts):
    parts.deploy_result = DeployResult(
        ok=False, error=err("healthcheck_failed", "헬스체크 실패"), details={"stderr": "last lines"}
    )


@pytest.mark.parametrize(
    "setup, stage, code, ran",
    [
        (build_fails, "build", "docker_build_failed", ["build"]),
        (foundation_fails, "foundation", "access_denied", ["build", "read"]),
        (check_fails, "check", "ssh_unreachable", ["build", "read", "check"]),
        (deploy_fails, "deploy", "healthcheck_failed", ["build", "read", "check", "deploy"]),
    ],
)
def test_a_failure_stops_at_that_stage_and_names_it(setup, stage, code, ran):
    parts = Parts()
    setup(parts)
    result, parts, log = run(parts)
    assert not result.ok and result.details["stage"] == stage and result.error.code == code
    assert parts.order == ran  # 실패한 단계 뒤는 실행되지 않고, 정리(prune)도 하지 않는다
    assert result.image_tag == SHA and any(e.level == "error" for e in log.events)


def test_the_tail_of_a_failed_build_and_the_adapters_details_are_kept():
    parts = Parts()
    build_fails(parts)
    assert run(parts)[0].details["stderr"] == "pip failed"
    parts = Parts()
    deploy_fails(parts)
    assert run(parts)[0].details["stderr"] == "last lines"


def test_a_failed_foundation_apply_is_reported_as_the_foundation_stage():
    parts = Parts()
    parts.runner_reads = [
        MISSING,
        TerraformError(err("terraform_read_after_apply", "다시 읽지 못했습니다.")),
    ]
    result, parts, _ = run(parts)
    assert result.details["stage"] == "foundation" and parts.order == [
        "build",
        "read",
        "apply",
        "read",
    ]


def test_an_unexpected_exception_becomes_a_pipeline_error_without_echoing_its_text():
    parts = Parts()
    parts.deploy_raises = RuntimeError(f"connection string password=hunter2 {USER_SECRET}")
    result, _, log = run(parts, secrets={"K": USER_SECRET})
    assert result.error.code == "deploy_pipeline_error" and result.details == {
        "exception": "RuntimeError",
        "stage": "deploy",
    }
    assert "hunter2" not in result.model_dump_json() and "hunter2" not in log.text()


# --- 정리 -------------------------------------------------------------------------------------
def test_a_failed_cleanup_never_fails_the_deploy_but_leaves_a_warning():
    parts = Parts()
    parts.prune_error = OSError("disk")
    result, _, log = run(parts)
    assert result.ok and result.details["pruned"] == []
    assert any(e.level == "warn" and e.name == "정리" for e in log.events)


# --- 비밀 ---------------------------------------------------------------------------------------
def test_user_secrets_are_masked_in_forwarded_logs_and_in_result_details():
    parts = Parts()
    parts.deploy_result = DeployResult(
        ok=False,
        error=err("healthcheck_failed", f"실패 {USER_SECRET}"),
        details={"stderr": f"echo {USER_SECRET}"},
    )
    result, _, log = run(parts, secrets={"K": USER_SECRET})
    assert USER_SECRET not in log.text() and USER_SECRET not in result.model_dump_json()
    assert "***" in log.text() and "***" in result.details["stderr"]


def test_a_secret_inside_a_successful_adapter_result_is_masked_too():
    parts = Parts()
    parts.deploy_result = DeployResult(
        ok=True, url=URL, image_tag=SHA, details={"warnings": [f"사용한 값 {USER_SECRET}"]}
    )
    result, _, _ = run(parts, secrets={"K": USER_SECRET})
    assert (
        result.ok
        and USER_SECRET not in result.model_dump_json()
        and "***" in result.details["warnings"][0]
    )


def test_a_successful_result_is_json_serializable_and_holds_no_secret():
    result, _, _ = run(secrets={"K": USER_SECRET})
    assert USER_SECRET not in result.model_dump_json()


def test_the_runner_and_the_foundation_settings_must_be_given_together():
    with pytest.raises(ValueError):
        Deployer({}, object(), runner=object())
    with pytest.raises(ValueError):
        Deployer({}, object(), foundation=SETTINGS)


# --- DNS ---------------------------------------------------------------------------------------
class FakeDns:
    """WildcardRecords(scope="aws")를 흉내 낸다."""

    def __init__(self, parts, changed=True, error=None, raises=None):
        self.parts, self.changed, self.error, self.raises, self.calls = (
            parts,
            changed,
            error,
            raises,
            [],
        )

    def name_for(self, env_id):
        return f"*.{env_id}.aws.anyship.cloud"

    def ensure(self, env_id, ip):
        self.parts.order.append("dns")
        self.calls.append((env_id, ip))
        if self.raises:
            raise self.raises
        if self.error:
            raise DnsError(self.error)
        return self.changed


def test_the_dns_record_is_set_after_the_foundation_is_read_and_before_the_connection_check():
    parts = Parts()
    dns = FakeDns(parts)
    result, parts, _ = run(parts, dns=dns)
    assert result.ok and parts.order == ["build", "read", "dns", "check", "deploy", "prune"]
    assert dns.calls == [
        ("test", "43.201.158.8")
    ]  # 서비스에 저장된 낡은 호스트(1.1.1.1)가 아니라 방금 읽은 값


def test_after_a_foundation_is_created_the_dns_record_points_at_the_new_host():
    parts = Parts()
    parts.runner_reads = [MISSING, "ok"]
    dns = FakeDns(parts)
    result, parts, _ = run(parts, dns=dns)
    assert result.ok and parts.order == [
        "build",
        "read",
        "apply",
        "read",
        "dns",
        "check",
        "deploy",
        "prune",
    ]


def test_the_dns_step_is_logged_inside_the_foundation_stage_and_stage_numbering_is_unchanged():
    parts = Parts()
    _, _, log = run(parts, dns=FakeDns(parts))
    messages = [e.message for e in log.events if e.name == "공용 기반 확인"]
    assert any("*.test.aws.anyship.cloud" in m and "43.201.158.8" in m for m in messages)
    assert any("새 주소로 맞췄습니다" in m for m in messages)
    assert {e.total for e in log.events if e.total} == {5}


def test_an_unchanged_record_is_reported_as_already_correct():
    parts = Parts()
    result, _, log = run(parts, dns=FakeDns(parts, changed=False))
    assert result.details["dns"] == {"record": "*.test.aws.anyship.cloud", "changed": False}
    assert any("이미 맞게" in e.message for e in log.events)


def test_the_result_tells_what_the_dns_step_did_and_says_nothing_when_there_is_none():
    parts = Parts()
    with_dns, _, _ = run(parts, dns=FakeDns(parts))
    assert with_dns.details["dns"] == {"record": "*.test.aws.anyship.cloud", "changed": True}
    without, _, _ = run()
    assert "dns" not in without.details


def test_a_failed_dns_update_stops_the_deploy_at_the_foundation_stage_with_its_own_code():
    parts = Parts()
    dns = FakeDns(
        parts,
        error=err(
            "dns_change_failed",
            "DNS 레코드를 바꾸지 못했습니다.",
            hint="권한을 확인해 주세요.",
            retryable=False,
        ),
    )
    result, parts, log = run(parts, dns=dns)
    assert not result.ok and result.details["stage"] == "foundation"
    assert (result.error.code, result.error.retryable, result.error.hint) == (
        "dns_change_failed",
        False,
        "권한을 확인해 주세요.",
    )
    assert parts.order == ["build", "read", "dns"]  # 연결 확인, 배포, 정리를 하지 않는다
    assert any(e.level == "error" for e in log.events)


def test_an_unexpected_dns_exception_becomes_a_foundation_stage_error_without_its_text():
    parts = Parts()
    result, _, _ = run(parts, dns=FakeDns(parts, raises=RuntimeError(f"leaked {USER_SECRET}")))
    assert result.error.code == "deploy_pipeline_error" and result.details == {
        "stage": "foundation",
        "exception": "RuntimeError",
    }
    assert USER_SECRET not in result.model_dump_json()


def test_dns_is_not_touched_when_the_foundation_itself_fails_or_for_onprem():
    parts = Parts()
    parts.runner_reads = [TerraformError(err("access_denied"))]
    dns = FakeDns(parts)
    result, parts, _ = run(parts, dns=dns)
    assert not result.ok and dns.calls == []
    onprem_parts = Parts()
    onprem_dns = FakeDns(onprem_parts)
    result, _, _ = run(onprem_parts, env=ONPREM_ENV, set_name="onprem", dns=onprem_dns)
    assert result.ok and onprem_dns.calls == []


def test_dns_needs_the_foundation_step_because_the_host_address_comes_from_it():
    with pytest.raises(ValueError):
        make_deployer(Parts(), runner=False, dns=FakeDns(Parts()))


# --- 삭제 --------------------------------------------------------------------------------------
def destroy(parts=None, env=ENV, app="todo", set_name="aws-always-on", **options):
    parts = parts or Parts()
    log = Log()
    result = make_deployer(parts, **options).destroy(env, app, log, set_name=set_name)
    return result, parts, log


def test_destroy_hands_the_environment_and_app_to_the_adapter_of_that_set():
    result, parts, _ = destroy()
    assert result.ok and parts.order == ["destroy"] and parts.destroys == [(ENV, "todo")]


def test_destroy_does_not_build_read_the_foundation_or_touch_dns():
    parts = Parts()
    result, parts, _ = destroy(parts, dns=FakeDns(parts))
    assert result.ok and parts.order == ["destroy"]  # 빌드, 기반 읽기, DNS, 정리는 하지 않는다


def test_destroy_logs_are_one_stage_with_the_adapters_own_numbering_kept_in_the_message():
    _, _, log = destroy()
    assert [(e.step, e.total, e.name) for e in log.events] == [(1, 1, "앱 삭제")]
    assert log.events[0].message.startswith("[2/2] ")


def test_a_failed_destroy_is_passed_on_unchanged_and_logged_as_an_error():
    parts = Parts()
    parts.destroy_result = DestroyResult(
        ok=False,
        error=err(
            "destroy_failed",
            "컨테이너를 지우지 못했습니다.",
            hint="docker compose down을 확인해 주세요.",
        ),
        details={"stderr": "boom"},
    )
    result, _, log = destroy(parts)
    assert not result.ok and (result.error.code, result.error.hint) == (
        "destroy_failed",
        "docker compose down을 확인해 주세요.",
    )
    assert result.details == {"stderr": "boom"}
    assert any(e.level == "error" and "지우지 못했습니다" in e.message for e in log.events)


def test_an_unexpected_exception_in_destroy_is_reported_without_its_text():
    parts = Parts()
    parts.destroy_raises = RuntimeError(f"leaked {USER_SECRET}")
    result, _, log = destroy(parts)
    assert result.error.code == "destroy_pipeline_error" and result.error.retryable
    assert result.details == {"exception": "RuntimeError"}
    assert USER_SECRET not in result.model_dump_json() and all(
        USER_SECRET not in e.message for e in log.events
    )


@pytest.mark.parametrize(
    "env, set_name", [(ENV, "onprem"), (ONPREM_ENV, "aws-always-on"), (ENV, "nope")]
)
def test_destroy_refuses_a_set_that_does_not_fit_the_environment_and_never_calls_the_adapter(
    env, set_name
):
    result, parts, _ = destroy(env=env, set_name=set_name)
    assert not result.ok and result.error.code == "set_not_supported" and parts.destroys == []


def test_an_onprem_environment_can_be_destroyed_through_its_own_set():
    result, parts, _ = destroy(env=ONPREM_ENV, set_name="onprem")
    assert result.ok and parts.destroys == [(ONPREM_ENV, "todo")]


def test_secret_looking_values_in_the_adapters_destroy_logs_are_masked():
    parts = Parts()
    token = (
        "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    )  # 비밀 스캐너가 진짜로 오해하지 않게 이어 붙인다
    parts.destroy_message = f"ssh 출력: token={token}"
    _, _, log = destroy(parts)
    assert token not in " ".join(e.message for e in log.events) and "***" in log.events[0].message


# --- 연결 확인(등록할 때) --------------------------------------------------------------------
def check(parts=None, env=ONPREM_ENV, set_name="onprem", **options):
    parts = parts or Parts()
    log = Log()
    result = make_deployer(parts, **options).check(env, log, set_name=set_name)
    return result, parts, log


def test_a_registration_check_asks_the_adapter_and_hands_back_what_it_found():
    parts = Parts()
    parts.check_result = CheckResult(ok=True, details={"public_ip": "203.0.113.5"})
    result, parts, _ = check(parts)
    assert result.ok and result.details == {"public_ip": "203.0.113.5"} and parts.order == ["check"]
    assert parts.checks == [ONPREM_ENV]


def test_a_registration_check_touches_nothing_else():
    parts = Parts()
    result, parts, _ = check(parts, dns=FakeDns(parts))
    assert parts.order == ["check"]  # 빌드, 기반 읽기, DNS, 배포를 하지 않는다


def test_the_check_logs_are_one_stage_with_the_adapters_numbering_kept_in_the_message():
    _, _, log = check()
    assert [(e.step, e.total, e.name) for e in log.events] == [(1, 1, "연결 확인")]
    assert log.events[0].message.startswith("[1/5] ")


def test_a_failed_check_is_passed_on_unchanged_with_its_hint_and_logged_as_an_error():
    parts = Parts()
    parts.check_result = CheckResult(
        ok=False,
        error=err(
            "docker_missing",
            "서버에 Docker가 없습니다.",
            hint="서버 준비 스크립트를 실행해 주세요.",
        ),
    )
    result, _, log = check(parts)
    assert not result.ok and (result.error.code, result.error.hint) == (
        "docker_missing",
        "서버 준비 스크립트를 실행해 주세요.",
    )
    assert any(e.level == "error" and "Docker가 없습니다" in e.message for e in log.events)


def test_an_unexpected_exception_in_a_check_is_reported_without_its_text():
    parts = Parts()
    parts.checks_raise = RuntimeError(f"leaked {USER_SECRET}")
    result, _, log = check(parts)
    assert result.error.code == "check_pipeline_error" and result.error.retryable
    assert result.details == {"exception": "RuntimeError"}
    assert USER_SECRET not in result.model_dump_json() and all(
        USER_SECRET not in e.message for e in log.events
    )


@pytest.mark.parametrize(
    "env, set_name", [(ONPREM_ENV, "aws-always-on"), (ENV, "onprem"), (ONPREM_ENV, "nope")]
)
def test_a_check_refuses_a_set_that_does_not_fit_the_environment_and_never_calls_the_adapter(
    env, set_name
):
    result, parts, _ = check(env=env, set_name=set_name)
    assert not result.ok and result.error.code == "set_not_supported" and parts.checks == []


def test_secret_looking_values_in_the_adapters_check_logs_are_masked():
    parts = Parts()
    token = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    parts.check_message = f"ssh 출력: token={token}"
    _, _, log = check(parts)
    assert token not in " ".join(e.message for e in log.events) and "***" in log.events[0].message


# --- 환경 정리(환경을 지울 때) ---------------------------------------------------------------
def remove_environment(parts=None, env=ONPREM_ENV, set_name="onprem", **options):
    parts = parts or Parts()
    log = Log()
    result = make_deployer(parts, **options).remove_environment(env, log, set_name=set_name)
    return result, parts, log


def test_removing_an_environment_asks_the_adapter_to_clean_up_what_it_made_for_the_environment():
    result, parts, _ = remove_environment()
    assert result.ok and parts.order == ["remove_environment"] and parts.cleanups == [ONPREM_ENV]


def test_removing_an_environment_does_not_destroy_apps_or_touch_dns_through_the_deployer():
    parts = Parts()
    _, parts, _ = remove_environment(parts, dns=FakeDns(parts))
    assert parts.order == ["remove_environment"] and parts.destroys == []


def test_the_environment_cleanup_logs_are_one_stage_with_the_adapters_numbering_kept_in_the_message():
    _, _, log = remove_environment()
    assert [(e.step, e.total, e.name) for e in log.events] == [(1, 1, "환경 정리")]
    assert log.events[0].message.startswith("[1/1] ")


def test_a_set_without_environment_level_cleanup_says_so_instead_of_pretending():
    result, parts, _ = remove_environment(env=ENV, set_name="aws-always-on")
    assert (
        not result.ok
        and result.error.code == "remove_environment_unsupported"
        and not result.error.retryable
    )
    assert parts.cleanups == [] and parts.destroys == []


def test_a_failed_environment_cleanup_is_passed_on_unchanged_and_logged_as_an_error():
    parts = Parts()
    parts.cleanup_result = DestroyResult(
        ok=False,
        error=err(
            "dns_change_failed",
            "DNS 레코드를 바꾸지 못했습니다.",
            hint="권한을 확인해 주세요.",
            retryable=True,
        ),
    )
    result, _, log = remove_environment(parts)
    assert not result.ok and (result.error.code, result.error.retryable) == (
        "dns_change_failed",
        True,
    )
    assert any(e.level == "error" for e in log.events)


def test_an_unexpected_exception_in_an_environment_cleanup_is_reported_without_its_text():
    parts = Parts()
    parts.cleanup_raises = RuntimeError(f"leaked {USER_SECRET}")
    result, _, _ = remove_environment(parts)
    assert result.error.code == "remove_environment_pipeline_error" and result.details == {
        "exception": "RuntimeError"
    }
    assert USER_SECRET not in result.model_dump_json()


@pytest.mark.parametrize(
    "env, set_name", [(ONPREM_ENV, "aws-always-on"), (ENV, "onprem"), (ONPREM_ENV, "nope")]
)
def test_an_environment_cleanup_refuses_a_set_that_does_not_fit_the_environment(env, set_name):
    result, parts, _ = remove_environment(env=env, set_name=set_name)
    assert not result.ok and result.error.code == "set_not_supported" and parts.cleanups == []

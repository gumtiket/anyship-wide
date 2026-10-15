import pytest
from fakes import Log

from anyship_adapters import AdapterError, AwsEnvironment
from anyship_adapters.foundation import (
    OPTIONAL_VARIABLES,
    Foundation,
    FoundationSettings,
    ensure_foundation,
)
from anyship_adapters.terraform_runner import TerraformError

BUCKET = "anyship-tfstate-223455088214-ap-northeast-2-2b9b6060"
ROLE = "arn:aws:iam::223455088214:role/deploy-service-role"
FILLED = dict(
    host="43.201.158.8",
    db_address="anyship-test-db.abc.ap-northeast-2.rds.amazonaws.com",
    db_port=5432,
    db_secret_arn="arn:aws:secretsmanager:ap-northeast-2:223455088214:secret:rds!db-abc-Xy1",
)
SETTINGS = FoundationSettings(
    service_server_ip="203.0.113.10",
    ssh_public_key="ssh-ed25519 AAAA deploy",
    acme_email="ops@example.com",
)


def env(**override):
    values = dict(
        env_id="test", role_arn=ROLE, external_id="ext-id-0123456789abcdef", state_bucket=BUCKET
    )
    return AwsEnvironment(**{**values, **override})


def failure(code, message="boom"):
    return TerraformError(AdapterError(code=code, message=message))


class Runner:
    """TerraformRunner를 흉내 낸다. reads는 read_foundation이 차례로 돌려줄 값(예외면 던진다)."""

    def __init__(self, reads, apply_error=None):
        self.reads, self.apply_error, self.calls, self.applied = list(reads), apply_error, [], []

    def read_foundation(self, environment, log):
        self.calls.append("read")
        result = self.reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return environment.model_copy(update=FILLED)

    def apply(self, environment, variables, log):
        self.calls.append("apply")
        self.applied.append(variables)
        if self.apply_error:
            raise self.apply_error


MISSING = failure("foundation_missing", "공용 기반이 아직 만들어지지 않았습니다.")


def test_an_existing_foundation_is_only_read_never_applied():
    runner, log = Runner(["ok"]), Log()
    result = ensure_foundation(runner, env(), SETTINGS, log)
    assert runner.calls == ["read"] and result.created is False
    assert result.env.host == "43.201.158.8" and result.fields() == FILLED


def test_an_empty_state_is_created_and_then_read_again():
    runner, log = Runner([MISSING, "ok"]), Log()
    result = ensure_foundation(runner, env(), SETTINGS, log)
    assert (
        runner.calls == ["read", "apply", "read"]
        and result.created is True
        and result.env.db_port == 5432
    )
    assert any("새로 만듭니다" in e.message and "20분" in e.message for e in log.events)


def test_the_variables_come_from_the_environment_and_the_service_settings():
    runner = Runner([MISSING, "ok"])
    ensure_foundation(runner, env(env_id="demo", region="ap-northeast-2"), SETTINGS, Log())
    assert runner.applied == [
        {
            "region": "ap-northeast-2",
            "account_id": "223455088214",
            "env_id": "demo",
            "service_server_ip": "203.0.113.10",
            "ssh_public_key": "ssh-ed25519 AAAA deploy",
            "acme_email": "ops@example.com",
        }
    ]


def test_optional_overrides_are_passed_on():
    settings = FoundationSettings(
        "203.0.113.10",
        "ssh-ed25519 AAAA deploy",
        "ops@example.com",
        {"host_instance_type": "t3.medium", "host_volume_size": 40},
    )
    runner = Runner([MISSING, "ok"])
    ensure_foundation(runner, env(), settings, Log())
    assert (
        runner.applied[0]["host_instance_type"] == "t3.medium"
        and runner.applied[0]["host_volume_size"] == 40
    )


@pytest.mark.parametrize(
    "name",
    [
        "region",
        "account_id",
        "env_id",
        "service_server_ip",
        "ssh_public_key",
        "acme_email",
        "unknown",
        "TF_VAR_x",
    ],
)
def test_required_or_unknown_variables_cannot_be_overridden(name):
    with pytest.raises(ValueError):
        FoundationSettings(
            "203.0.113.10", "ssh-ed25519 AAAA deploy", "ops@example.com", {name: "x"}
        )


def test_every_optional_variable_exists_in_the_terraform_module():
    # 서비스가 쓸 수 있는 변수 이름이 실제 모듈에 있는 이름과 같아야 한다.
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "user-account" / "variables.tf").read_text(
        encoding="utf-8"
    )
    assert all(f'variable "{name}"' in text for name in OPTIONAL_VARIABLES)
    required = {
        "region",
        "account_id",
        "env_id",
        "service_server_ip",
        "ssh_public_key",
        "acme_email",
    }
    assert all(f'variable "{name}"' in text for name in required)


def test_a_missing_state_bucket_is_never_treated_as_a_missing_foundation():
    runner = Runner([MISSING])
    with pytest.raises(TerraformError) as caught:
        ensure_foundation(runner, env(state_bucket=None), SETTINGS, Log())
    assert caught.value.error.code == "foundation_missing" and runner.applied == []


@pytest.mark.parametrize(
    "code",
    [
        "access_denied",
        "service_credentials_unavailable",
        "aws_unavailable",
        "terraform_locked",
        "terraform_output_invalid",
        "terraform_init_failed",
        "terraform_output_failed",
        "terraform_not_found",
        "terraform_timeout",
    ],
)
def test_any_other_read_error_stops_before_a_twenty_minute_apply(code):
    runner = Runner([failure(code)])
    with pytest.raises(TerraformError) as caught:
        ensure_foundation(runner, env(), SETTINGS, Log())
    assert caught.value.error.code == code and runner.calls == ["read"]


def test_a_failed_apply_is_reported_and_the_foundation_is_not_read_afterwards():
    runner = Runner([MISSING], apply_error=failure("terraform_apply_failed"))
    with pytest.raises(TerraformError) as caught:
        ensure_foundation(runner, env(), SETTINGS, Log())
    assert caught.value.error.code == "terraform_apply_failed" and runner.calls == ["read", "apply"]


def test_a_state_that_is_still_empty_after_apply_is_reported_not_retried():
    runner = Runner([MISSING, MISSING])
    with pytest.raises(TerraformError) as caught:
        ensure_foundation(runner, env(), SETTINGS, Log())
    assert caught.value.error.code == "foundation_missing" and runner.calls == [
        "read",
        "apply",
        "read",
    ]


def test_calling_again_after_an_interrupted_apply_applies_again():
    # 도중에 끊긴 apply는 출력이 아직 기록되기 전이라 state가 비어 보인다. 다시 부르면 이어서 만든다.
    first = Runner([MISSING], apply_error=failure("terraform_timeout"))
    with pytest.raises(TerraformError):
        ensure_foundation(first, env(), SETTINGS, Log())
    second = Runner([MISSING, "ok"])
    assert ensure_foundation(second, env(), SETTINGS, Log()).created is True and second.calls == [
        "read",
        "apply",
        "read",
    ]


def test_the_result_is_a_new_environment_and_the_original_is_untouched():
    original = env()
    result = ensure_foundation(Runner(["ok"]), original, SETTINGS, Log())
    assert isinstance(result, Foundation) and original.host is None and result.env is not original

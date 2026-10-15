import json
from datetime import datetime, timezone

import pytest

boto3 = pytest.importorskip(
    "boto3"
)  # AWS 기능은 선택 의존성이라, 없으면 이 파일의 테스트만 건너뛴다
from botocore.exceptions import NoCredentialsError  # noqa: E402
from botocore.stub import ANY, Stubber  # noqa: E402

from anyship_adapters.aws_access import AwsAccess, AwsAccessError, TemporaryCredentials  # noqa: E402
from anyship_adapters.models import AwsEnvironment  # noqa: E402
from anyship_adapters.redact import redact_text  # noqa: E402

ACCOUNT = "223455088214"
EXTERNAL_ID = "ext-id-0123456789abcdef"
SECRET_ARN = f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:rds!db-0a1b2c3d-AbCdEf"
MASTER = "M4ster-Pass-0123456789"
# 비밀 스캐너가 진짜 키로 오해하지 않도록 AKIA/ASIA로 시작하지 않는 가짜 값을 쓴다.
CREDS = {
    "AccessKeyId": "TESTKEYID0123456789",
    "SecretAccessKey": "test-secret-key",
    "SessionToken": "test-token",
    "Expiration": datetime(2026, 10, 10, tzinfo=timezone.utc),
}


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    # 진짜 자격 증명이 없어도 클라이언트를 만들 수 있게 하고, 실수로 진짜 AWS를 부르는 일을 막는다.
    for key, value in {
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "ap-northeast-2",
    }.items():
        monkeypatch.setenv(key, value)


def env(**override):
    values = dict(
        env_id="test",
        role_arn=f"arn:aws:iam::{ACCOUNT}:role/deploy-service-role",
        external_id=EXTERNAL_ID,
        db_secret_arn=SECRET_ARN,
    )
    return AwsEnvironment(**{**values, **override})


class FakeSession:
    def __init__(self, clients):
        self.clients, self.requested = clients, []

    def client(self, name, **kwargs):
        self.requested.append((name, kwargs))
        return self.clients[name]


def stubbed(service):
    client = boto3.client(service, region_name="ap-northeast-2")
    return client, Stubber(client)


def make(base=None, assumed=None):
    """base는 서비스 서버의 자격 증명, assumed는 역할을 맡은 뒤의 자격 증명으로 만든 세션. (access, 세션 생성 기록)"""
    created = []

    def factory(**credentials):
        created.append(credentials)
        return FakeSession(assumed if credentials else base)

    return AwsAccess(factory), created


def assume_ok(stub):
    stub.add_response(
        "assume_role",
        {"Credentials": CREDS},
        {
            "RoleArn": env().role_arn,
            "ExternalId": EXTERNAL_ID,
            "DurationSeconds": 900,
            "RoleSessionName": ANY,
        },
    )


def identity(stub, account=ACCOUNT):
    stub.add_response(
        "get_caller_identity",
        {
            "Account": account,
            "Arn": f"arn:aws:sts::{account}:assumed-role/r/s",
            "UserId": "AROATEST:s",
        },
    )


# --- check_role ------------------------------------------------------------------------------
def test_check_role_assumes_the_role_with_the_external_id_and_returns_the_account():
    sts, sts_stub = stubbed("sts")
    assumed_sts, assumed_stub = stubbed("sts")
    assume_ok(sts_stub)
    identity(assumed_stub)
    access, created = make({"sts": sts}, {"sts": assumed_sts})
    with sts_stub, assumed_stub:
        assert access.check_role(env()) == ACCOUNT
    # 두 번째 세션은 역할을 맡아 받은 임시 자격 증명으로 만들어야 한다.
    assert created[1]["aws_access_key_id"] == CREDS["AccessKeyId"]
    assert created[1]["aws_session_token"] == CREDS["SessionToken"]


def test_a_role_in_another_account_than_the_verified_one_is_rejected():
    sts, sts_stub = stubbed("sts")
    assumed_sts, assumed_stub = stubbed("sts")
    assume_ok(sts_stub)
    identity(assumed_stub, account="111111111111")
    access, _ = make({"sts": sts}, {"sts": assumed_sts})
    with sts_stub, assumed_stub, pytest.raises(AwsAccessError) as caught:
        access.check_role(env())
    assert caught.value.error.code == "account_mismatch"


@pytest.mark.parametrize(
    "aws_code, status, code, retryable",
    [
        ("AccessDenied", 403, "access_denied", False),
        ("ExpiredToken", 400, "service_credentials_unavailable", False),
        ("InvalidClientTokenId", 403, "service_credentials_unavailable", False),
        ("Throttling", 400, "aws_unavailable", True),
    ],
)
def test_aws_errors_become_codes_that_never_echo_the_arn_or_external_id(
    aws_code, status, code, retryable
):
    sts, stub = stubbed("sts")
    stub.add_client_error(
        "assume_role",
        service_error_code=aws_code,
        http_status_code=status,
        service_message=f"role {env().role_arn} external {EXTERNAL_ID}",
    )
    access, _ = make({"sts": sts})
    with stub, pytest.raises(AwsAccessError) as caught:
        access.check_role(env())
    error = caught.value.error
    assert (error.code, error.retryable) == (code, retryable)
    assert (
        EXTERNAL_ID not in error.model_dump_json() and env().role_arn not in error.model_dump_json()
    )


def test_missing_service_credentials_are_reported_as_such():
    class NoCredentials:
        def assume_role(self, **kwargs):
            raise NoCredentialsError()

    access, _ = make({"sts": NoCredentials()})
    with pytest.raises(AwsAccessError) as caught:
        access.check_role(env())
    assert caught.value.error.code == "service_credentials_unavailable"


# --- read_master_password --------------------------------------------------------------------
def secret_stubs(secret_string):
    sts, sts_stub = stubbed("sts")
    secrets, secrets_stub = stubbed("secretsmanager")
    assume_ok(sts_stub)
    secrets_stub.add_response(
        "get_secret_value",
        {"ARN": SECRET_ARN, "Name": "rds!db-0a1b2c3d", "SecretString": secret_string},
        {"SecretId": SECRET_ARN},
    )
    access, created = make({"sts": sts}, {"secretsmanager": secrets})
    return access, created, sts_stub, secrets_stub


def test_the_master_password_is_read_with_the_assumed_role_in_the_secrets_region():
    secret = json.dumps({"username": "anyship_admin", "password": MASTER})
    access, created, sts_stub, secrets_stub = secret_stubs(secret)
    with sts_stub, secrets_stub:
        assert access.read_master_password(env()) == MASTER
    assert created[1]["aws_session_token"] == CREDS["SessionToken"]


@pytest.mark.parametrize(
    "arn", [None, "arn:aws:secretsmanager:ap-northeast-2:111111111111:secret:rds!db-x"]
)
def test_a_missing_or_foreign_secret_arn_is_rejected_before_any_aws_call(arn):
    calls = []
    access = AwsAccess(lambda **kwargs: calls.append(kwargs))
    with pytest.raises(AwsAccessError) as caught:
        access.read_master_password(
            env().model_copy(update={"db_secret_arn": arn})
        )  # 모델 검증을 건너뛴 객체
    assert caught.value.error.code == "invalid_secret_arn" and calls == []


@pytest.mark.parametrize(
    "secret",
    [
        '{"username": "u"}',
        "not json",
        json.dumps({"password": "a\nb"}),
        json.dumps({"password": ""}),
        json.dumps({"password": 1234}),
    ],
)
def test_an_unusable_secret_is_rejected_without_leaking_its_content(secret):
    access, _, sts_stub, secrets_stub = secret_stubs(secret)
    with sts_stub, secrets_stub, pytest.raises(AwsAccessError) as caught:
        access.read_master_password(env())
    assert caught.value.error.code == "secret_unreadable"
    assert secret not in caught.value.error.model_dump_json()


# --- temporary_credentials (Terraform 하위 프로세스용) -----------------------------------------------
def assume_for(stub, seconds):
    stub.add_response(
        "assume_role",
        {"Credentials": CREDS},
        {
            "RoleArn": env().role_arn,
            "ExternalId": EXTERNAL_ID,
            "DurationSeconds": seconds,
            "RoleSessionName": ANY,
        },
    )


def test_temporary_credentials_default_to_an_hour_and_become_three_environment_variables():
    sts, stub = stubbed("sts")
    assume_for(stub, 3600)
    access, created = make({"sts": sts})
    with stub:
        credentials = access.temporary_credentials(env())
    assert isinstance(credentials, TemporaryCredentials)
    assert credentials.environ() == {
        "AWS_ACCESS_KEY_ID": CREDS["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": CREDS["SecretAccessKey"],
        "AWS_SESSION_TOKEN": CREDS["SessionToken"],
    }
    assert created == [{}]  # 세션을 따로 만들지 않고 STS 호출만 한다


def test_a_shorter_duration_is_passed_to_aws():
    sts, stub = stubbed("sts")
    assume_for(stub, 1800)
    access, _ = make({"sts": sts})
    with stub:
        access.temporary_credentials(env(), 1800)


@pytest.mark.parametrize("seconds", [0, 899, 3601, 43200])
def test_a_duration_outside_fifteen_minutes_to_an_hour_is_refused_before_calling_aws(seconds):
    calls = []
    access = AwsAccess(lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError):
        access.temporary_credentials(env(), seconds)
    assert calls == []


def test_the_credentials_never_show_up_in_repr_or_str():
    sts, stub = stubbed("sts")
    assume_for(stub, 3600)
    access, _ = make({"sts": sts})
    with stub:
        credentials = access.temporary_credentials(env())
    for text in (repr(credentials), str(credentials), f"{credentials}", f"{credentials!r}"):
        assert all(value not in text for value in CREDS.values() if isinstance(value, str))


def test_every_secret_value_can_be_registered_for_masking():
    sts, stub = stubbed("sts")
    assume_for(stub, 3600)
    access, _ = make({"sts": sts})
    with stub:
        credentials = access.temporary_credentials(env())
    leaked = f"ERROR key={CREDS['AccessKeyId']} secret={CREDS['SecretAccessKey']} token={CREDS['SessionToken']}"
    masked = redact_text(leaked, credentials.secret_values())
    assert all(
        value not in masked
        for value in (CREDS["AccessKeyId"], CREDS["SecretAccessKey"], CREDS["SessionToken"])
    )


@pytest.mark.parametrize(
    "aws_code, status, code, retryable",
    [
        ("AccessDenied", 403, "access_denied", False),
        ("ExpiredToken", 400, "service_credentials_unavailable", False),
        ("Throttling", 400, "aws_unavailable", True),
    ],
)
def test_aws_errors_for_the_terraform_path_use_the_same_codes_and_never_echo_the_arn_or_external_id(
    aws_code, status, code, retryable
):
    sts, stub = stubbed("sts")
    stub.add_client_error(
        "assume_role",
        service_error_code=aws_code,
        http_status_code=status,
        service_message=f"role {env().role_arn} external {EXTERNAL_ID}",
    )
    access, _ = make({"sts": sts})
    with stub, pytest.raises(AwsAccessError) as caught:
        access.temporary_credentials(env())
    error = caught.value.error
    assert (error.code, error.retryable) == (code, retryable)
    assert (
        EXTERNAL_ID not in error.model_dump_json() and env().role_arn not in error.model_dump_json()
    )


def test_a_response_without_the_expected_fields_is_reported_without_echoing_it():
    class Incomplete:
        def assume_role(self, **kwargs):
            return {"Credentials": {"AccessKeyId": CREDS["AccessKeyId"]}}  # 비밀 키와 토큰이 없다

    access, _ = make({"sts": Incomplete()})
    with pytest.raises(AwsAccessError) as caught:
        access.temporary_credentials(env())
    assert caught.value.error.code == "aws_unavailable" and caught.value.error.retryable
    assert CREDS["AccessKeyId"] not in caught.value.error.model_dump_json()


# --- read_state_bucket (온보딩 스택 출력) --------------------------------------------------------
STACK = "anyship-onboarding-0123456789abcdef0123456789abcdef"
BUCKET = f"anyship-tfstate-{ACCOUNT}-ap-northeast-2-2b9b6060"


def stack(status="CREATE_COMPLETE", outputs=None):
    outputs = (
        [
            {"OutputKey": "RoleArn", "OutputValue": env().role_arn},
            {"OutputKey": "StateBucketName", "OutputValue": BUCKET},
        ]
        if outputs is None
        else outputs
    )
    found = {
        "StackName": STACK,
        "CreationTime": datetime(2026, 10, 10, tzinfo=timezone.utc),
        "StackStatus": status,
    }
    return {"Stacks": [{**found, "Outputs": outputs}]}


def stack_stubs(response=None, *, error=None):
    sts, sts_stub = stubbed("sts")
    formation, formation_stub = stubbed("cloudformation")
    assume_ok(sts_stub)
    if error:
        formation_stub.add_client_error("describe_stacks", **error)
    else:
        formation_stub.add_response("describe_stacks", response or stack(), {"StackName": STACK})
    access, created = make({"sts": sts}, {"cloudformation": formation})
    return access, created, sts_stub, formation_stub


def test_the_state_bucket_is_read_from_the_stack_output_with_the_assumed_role():
    access, created, sts_stub, formation_stub = stack_stubs()
    with sts_stub, formation_stub:
        assert access.read_state_bucket(env(), STACK) == BUCKET
    assert created[1]["aws_session_token"] == CREDS["SessionToken"]


@pytest.mark.parametrize(
    "status", ["CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"]
)
def test_a_finished_stack_is_accepted(status):
    access, _, sts_stub, formation_stub = stack_stubs(stack(status))
    with sts_stub, formation_stub:
        assert access.read_state_bucket(env(), STACK) == BUCKET


@pytest.mark.parametrize(
    "status",
    [
        "CREATE_IN_PROGRESS",
        "ROLLBACK_COMPLETE",
        "ROLLBACK_IN_PROGRESS",
        "CREATE_FAILED",
        "DELETE_IN_PROGRESS",
        "UPDATE_IN_PROGRESS",
    ],
)
def test_a_stack_that_is_unfinished_or_failed_is_not_trusted(status):
    access, _, sts_stub, formation_stub = stack_stubs(stack(status))
    with sts_stub, formation_stub, pytest.raises(AwsAccessError) as caught:
        access.read_state_bucket(env(), STACK)
    assert caught.value.error.code == "stack_not_ready" and caught.value.error.retryable


@pytest.mark.parametrize("name", ["", "1abc", "a b", "x;rm -rf", "anyship/onboarding", "a" * 129])
def test_an_invalid_stack_name_is_rejected_before_any_aws_call(name):
    calls = []
    access = AwsAccess(lambda **kwargs: calls.append(kwargs))
    with pytest.raises(AwsAccessError) as caught:
        access.read_state_bucket(env(), name)
    assert caught.value.error.code == "invalid_stack_name" and calls == []


def test_a_missing_stack_is_reported_as_such_without_echoing_its_name():
    access, _, sts_stub, formation_stub = stack_stubs(
        error=dict(
            service_error_code="ValidationError",
            http_status_code=400,
            service_message=f"Stack with id {STACK} does not exist",
        )
    )
    with sts_stub, formation_stub, pytest.raises(AwsAccessError) as caught:
        access.read_state_bucket(env(), STACK)
    assert caught.value.error.code == "stack_not_found" and not caught.value.error.retryable
    assert STACK not in caught.value.error.model_dump_json()


def test_an_empty_stack_list_is_a_missing_stack():
    access, _, sts_stub, formation_stub = stack_stubs({"Stacks": []})
    with sts_stub, formation_stub, pytest.raises(AwsAccessError) as caught:
        access.read_state_bucket(env(), STACK)
    assert caught.value.error.code == "stack_not_found"


@pytest.mark.parametrize(
    "aws_code, status, code, retryable",
    [
        ("AccessDenied", 403, "access_denied", False),
        ("Throttling", 400, "aws_unavailable", True),
    ],
)
def test_other_describe_errors_use_the_same_codes(aws_code, status, code, retryable):
    access, _, sts_stub, formation_stub = stack_stubs(
        error=dict(
            service_error_code=aws_code,
            http_status_code=status,
            service_message=f"{STACK} {EXTERNAL_ID}",
        )
    )
    with sts_stub, formation_stub, pytest.raises(AwsAccessError) as caught:
        access.read_state_bucket(env(), STACK)
    error = caught.value.error
    assert (error.code, error.retryable) == (code, retryable)
    assert STACK not in error.model_dump_json() and EXTERNAL_ID not in error.model_dump_json()


@pytest.mark.parametrize(
    "outputs",
    [
        [],  # 출력이 없다
        [
            {"OutputKey": "RoleArn", "OutputValue": "arn:aws:iam::223455088214:role/x"}
        ],  # 필요한 키가 없다
        [
            {"OutputKey": "StateBucketName", "OutputValue": "my-own-bucket"}
        ],  # 우리가 짓는 모양이 아니다
        [
            {
                "OutputKey": "StateBucketName",
                "OutputValue": "anyship-tfstate-111111111111-ap-northeast-2-2b9b6060",
            }
        ],  # 다른 계정
        [{"OutputKey": "StateBucketName", "OutputValue": f"{BUCKET}; rm -rf /"}],
        [{"OutputKey": "StateBucketName"}],  # 값이 없다
    ],
)
def test_a_bucket_output_that_is_missing_or_not_ours_is_rejected_without_echoing_it(outputs):
    access, _, sts_stub, formation_stub = stack_stubs(stack(outputs=outputs))
    with sts_stub, formation_stub, pytest.raises(AwsAccessError) as caught:
        access.read_state_bucket(env(), STACK)
    error = caught.value.error
    assert error.code == "stack_output_invalid"
    assert (
        "my-own-bucket" not in error.model_dump_json()
        and "111111111111" not in error.model_dump_json()
    )


def test_the_stack_is_read_in_the_environment_region(monkeypatch):
    requested = []
    original = FakeSession.client

    def record(self, name, **kwargs):
        requested.append((name, kwargs))
        return original(self, name, **kwargs)

    monkeypatch.setattr(FakeSession, "client", record)
    access, _, sts_stub, formation_stub = stack_stubs()
    with sts_stub, formation_stub:
        access.read_state_bucket(env(region="us-east-1"), STACK)
    assert ("cloudformation", {"region_name": "us-east-1"}) in requested

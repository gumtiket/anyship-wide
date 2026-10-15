"""Use botocore's Stubber: no AWS credentials, metadata, or network calls."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import NoCredentialsError, ReadTimeoutError
from botocore.stub import Stubber

from app import aws_sts_adapter as aws_adapter
from app.aws_adapter import AwsCheckError
from app.aws_sts_adapter import STSAdapter

ROLE = "arn:aws:iam::123456789012:role/path/deploy-service-role"
EXTERNAL = "e" * 64
REGION = "ap-northeast-2"
IDENTIFIER = uuid.UUID("11111111-1111-1111-1111-111111111111")
PARAMS = {
    "RoleArn": ROLE,
    "RoleSessionName": "anyship-check-" + IDENTIFIER.hex,
    "DurationSeconds": 900,
}
CREDENTIALS = {
    "AccessKeyId": "ASIA" + "X" * 16,
    "SecretAccessKey": "S" * 40,
    "SessionToken": "PRIVATE-SESSION-TOKEN",
    "Expiration": datetime(2030, 1, 1, tzinfo=timezone.utc),
}


@pytest.fixture
def sts(monkeypatch):
    real_session = boto3.session.Session(
        aws_access_key_id="A" * 20, aws_secret_access_key="B" * 40, region_name=REGION
    )
    source, target = real_session.client("sts"), real_session.client("sts")
    source_stub, target_stub = Stubber(source), Stubber(target)
    factory = Mock(side_effect=[source, target])
    monkeypatch.setattr(aws_adapter.boto3, "Session", lambda: SimpleNamespace(client=factory))
    monkeypatch.setattr(aws_adapter.uuid, "uuid4", lambda: IDENTIFIER)
    with source_stub, target_stub:
        yield source_stub, target_stub, factory
        source_stub.assert_no_pending_responses()
        target_stub.assert_no_pending_responses()
    source.close()
    target.close()


def positive(stub):
    stub.add_response(
        "assume_role", {"Credentials": CREDENTIALS}, {**PARAMS, "ExternalId": EXTERNAL}
    )


def denied(stub, extra=None, code="AccessDenied"):
    stub.add_client_error(
        "assume_role",
        service_error_code=code,
        service_message="PRIVATE-AWS-MESSAGE",
        http_status_code=403,
        expected_params={**PARAMS, **(extra or {})},
    )


def run_check():
    return STSAdapter().check(role_arn=ROLE, external_id=EXTERNAL, region=REGION)


def test_role_identity_and_external_id_enforcement(sts, caplog):
    source, target, factory = sts
    positive(source)
    denied(source)
    denied(source, {"ExternalId": "invalid-" + IDENTIFIER.hex})
    target.add_response(
        "get_caller_identity",
        {
            "Account": "123456789012",
            "UserId": "AROAEXAMPLE:session",
            "Arn": "arn:aws:sts::123456789012:assumed-role/deploy-service-role/session",
        },
        {},
    )
    result = run_check()
    assert result.account_id == "123456789012"
    assert "PRIVATE-SESSION-TOKEN" not in repr(result) + caplog.text
    calls = factory.call_args_list
    assert len(calls) == 2 and "aws_access_key_id" not in calls[0].kwargs
    assert calls[1].kwargs["aws_access_key_id"] == CREDENTIALS["AccessKeyId"]
    assert calls[1].kwargs["aws_session_token"] == CREDENTIALS["SessionToken"]
    assert calls[0].kwargs["region_name"] == REGION
    assert calls[0].kwargs["config"].retries["total_max_attempts"] == 2


@pytest.mark.parametrize("which", ["missing", "incorrect"])
def test_unsafe_external_id_policy_rejected(sts, which):
    source, _, factory = sts
    positive(source)
    extra = {}
    if which == "incorrect":
        denied(source)
        extra = {"ExternalId": "invalid-" + IDENTIFIER.hex}
    source.add_response("assume_role", {"Credentials": CREDENTIALS}, {**PARAMS, **extra})
    with pytest.raises(AwsCheckError) as captured:
        run_check()
    assert captured.value.code == "external_id_not_required"
    assert factory.call_count == 1


@pytest.mark.parametrize(
    "code,expected",
    [
        ("AccessDenied", "access_denied"),
        ("Throttling", "aws_unavailable"),
        ("ExpiredToken", "service_credentials_unavailable"),
    ],
)
def test_service_errors_sanitized(sts, code, expected):
    source, _, _ = sts
    denied(source, {"ExternalId": EXTERNAL}, code)
    with pytest.raises(AwsCheckError) as captured:
        run_check()
    assert captured.value.code == expected
    assert "PRIVATE-AWS-MESSAGE" not in str(captured.value)


def test_negative_probe_throttling_is_not_treated_as_safe_denial(sts):
    source, _, _ = sts
    positive(source)
    denied(source, code="Throttling")
    with pytest.raises(AwsCheckError) as captured:
        run_check()
    assert captured.value.code == "aws_unavailable"


def test_account_mismatch(sts):
    source, target, _ = sts
    positive(source)
    denied(source)
    denied(source, {"ExternalId": "invalid-" + IDENTIFIER.hex})
    target.add_response(
        "get_caller_identity",
        {
            "Account": "999999999999",
            "UserId": "AROAEXAMPLE:session",
            "Arn": "arn:aws:sts::999999999999:assumed-role/name/session",
        },
        {},
    )
    with pytest.raises(AwsCheckError) as captured:
        run_check()
    assert captured.value.code == "account_mismatch"


@pytest.mark.parametrize(
    "error,expected",
    [
        (NoCredentialsError(), "service_credentials_unavailable"),
        (
            ReadTimeoutError(endpoint_url="https://sts.ap-northeast-2.amazonaws.com"),
            "aws_unavailable",
        ),
    ],
)
def test_missing_credentials_and_network_errors(monkeypatch, error, expected):
    factory = Mock(side_effect=error)
    monkeypatch.setattr(aws_adapter.boto3, "Session", lambda: SimpleNamespace(client=factory))
    with pytest.raises(AwsCheckError) as captured:
        run_check()
    assert captured.value.code == expected


def test_bad_role_rejected_before_creating_aws_client(monkeypatch):
    factory = Mock(side_effect=AssertionError("must not initialize AWS"))
    monkeypatch.setattr(aws_adapter.boto3, "Session", factory)
    with pytest.raises(ValueError):
        STSAdapter().check(
            role_arn="arn:aws:sts::123456789012:assumed-role/name/session",
            external_id=EXTERNAL,
            region=REGION,
        )
    factory.assert_not_called()

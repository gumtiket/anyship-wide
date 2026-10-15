import re
import uuid

import pytest

from anyship_adapters.models import ENV_ID_PATTERN
from app.db import AwsEnvironment, Base, User, Workspace, database
from app.deploy_state import (
    DeployStateError,
    adapter_environment,
    assign_env_id,
    ensure_state_bucket,
    save_foundation,
)

ACCOUNT = "223455088214"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/deploy-service-role"
BUCKET = f"anyship-tfstate-{ACCOUNT}-ap-northeast-2-2b9b6060"
FOUNDATION = dict(
    host="43.201.158.8",
    db_address="anyship-test-db.abc.ap-northeast-2.rds.amazonaws.com",
    db_port=5432,
    db_secret_arn=f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:rds!db-abc-Xy1",
)


@pytest.fixture
def sessions(database_url):
    engine, factory = database(database_url)
    Base.metadata.create_all(engine)
    with factory() as session:
        session.add_all(
            [User(id="u1", github_id=1, login="u", name="사용자"), Workspace(id="w1", name="w")]
        )
        session.commit()
    yield factory
    engine.dispose()


def add(sessions, **override):
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
        stack_name="anyship-onboarding-" + identifier.replace("-", ""),
        role_name="deploy-service-role",
        status="CONNECTED",
        role_arn=ROLE,
        aws_account_id=ACCOUNT,
        created_at=1,
        expires_at=2,
    )
    with sessions() as session:
        session.add(AwsEnvironment(**{**values, **override}))
        session.commit()
    return values["id"]


def reload(sessions, identifier):
    with sessions() as session:
        return session.get(AwsEnvironment, identifier)


class Access:
    def __init__(self, bucket=BUCKET, error=None):
        self.bucket, self.error, self.calls = bucket, error, []

    def read_state_bucket(self, env, stack_name):
        self.calls.append((env, stack_name))
        if self.error:
            raise self.error
        return self.bucket


# --- assign_env_id ---------------------------------------------------------------------------
def test_the_env_id_comes_from_the_record_id_and_fits_the_adapter_rule(sessions):
    identifier = add(sessions)
    with sessions() as session:
        value = assign_env_id(session, session.get(AwsEnvironment, identifier))
    assert value == "e" + identifier.replace("-", "")[:20] and len(value) == 21
    assert re.match(ENV_ID_PATTERN, value) and reload(sessions, identifier).env_id == value


def test_an_existing_env_id_is_never_changed(sessions):
    identifier = add(sessions, env_id="test")
    with sessions() as session:
        assert assign_env_id(session, session.get(AwsEnvironment, identifier)) == "test"
    assert reload(sessions, identifier).env_id == "test"


def test_different_environments_get_different_ids(sessions):
    ids = []
    for number in range(3):  # 같은 워크스페이스에서는 Role ARN이 겹칠 수 없다
        identifier = add(
            sessions, role_arn=f"arn:aws:iam::{ACCOUNT}:role/deploy-service-role-{number}"
        )
        with sessions() as session:
            ids.append(assign_env_id(session, session.get(AwsEnvironment, identifier)))
    assert len(set(ids)) == 3


# --- adapter_environment ---------------------------------------------------------------------
def test_a_connected_environment_becomes_an_adapter_environment_with_only_the_values_it_has(
    sessions,
):
    identifier = add(sessions, env_id="test")
    env = adapter_environment(reload(sessions, identifier))
    assert (env.env_id, env.role_arn, env.region) == ("test", ROLE, "ap-northeast-2")
    assert env.external_id == reload(sessions, identifier).external_id
    assert (
        env.host is None
        and env.db_address is None
        and env.state_bucket is None
        and env.db_port == 5432
    )


def test_stored_foundation_values_are_passed_on(sessions):
    identifier = add(sessions, env_id="test", state_bucket=BUCKET, **FOUNDATION)
    env = adapter_environment(reload(sessions, identifier))
    assert (env.host, env.db_address, env.db_secret_arn, env.state_bucket) == (
        FOUNDATION["host"],
        FOUNDATION["db_address"],
        FOUNDATION["db_secret_arn"],
        BUCKET,
    )


@pytest.mark.parametrize("status", ["PENDING", "VERIFYING", "FAILED", "EXPIRED"])
def test_only_a_verified_connection_can_be_used_even_if_a_role_was_submitted(sessions, status):
    identifier = add(sessions, env_id="test", status=status, submitted_role_arn=ROLE)
    with pytest.raises(DeployStateError) as caught:
        adapter_environment(reload(sessions, identifier))
    assert caught.value.code == "environment_not_connected"


def test_a_connected_row_without_a_role_is_refused(sessions):
    identifier = add(
        sessions, env_id="test", role_arn=None, aws_account_id=None, submitted_role_arn=ROLE
    )
    with pytest.raises(DeployStateError) as caught:
        adapter_environment(reload(sessions, identifier))
    assert caught.value.code == "environment_not_connected"


def test_an_environment_without_an_env_id_is_refused(sessions):
    with pytest.raises(DeployStateError) as caught:
        adapter_environment(reload(sessions, add(sessions)))
    assert caught.value.code == "env_id_missing"


def test_stored_values_that_break_the_adapter_rules_are_reported_without_echoing_them(sessions):
    identifier = add(sessions, env_id="test", host="bad host; rm -rf /")
    with pytest.raises(DeployStateError) as caught:
        adapter_environment(reload(sessions, identifier))
    assert caught.value.code == "environment_invalid" and "rm -rf" not in caught.value.message


# --- ensure_state_bucket ---------------------------------------------------------------------
def test_a_stored_state_bucket_is_used_without_calling_aws(sessions):
    identifier = add(sessions, env_id="test", state_bucket=BUCKET)
    access = Access(bucket="never-used")
    with sessions() as session:
        assert (
            ensure_state_bucket(session, session.get(AwsEnvironment, identifier), access) == BUCKET
        )
    assert access.calls == []


def test_a_missing_state_bucket_is_read_with_the_stack_name_and_stored(sessions):
    identifier = add(sessions, env_id="test")
    access = Access()
    with sessions() as session:
        row = session.get(AwsEnvironment, identifier)
        assert ensure_state_bucket(session, row, access) == BUCKET
    ((env, stack_name),) = access.calls
    assert stack_name == reload(sessions, identifier).stack_name and env.role_arn == ROLE
    assert reload(sessions, identifier).state_bucket == BUCKET


def test_a_failed_read_stores_nothing_and_is_not_swallowed(sessions):
    identifier = add(sessions, env_id="test")
    access = Access(error=RuntimeError("stack_not_found"))
    with sessions() as session, pytest.raises(RuntimeError):
        ensure_state_bucket(session, session.get(AwsEnvironment, identifier), access)
    assert reload(sessions, identifier).state_bucket is None


def test_aws_is_not_called_for_an_environment_that_is_not_connected(sessions):
    identifier = add(sessions, env_id="test", status="PENDING")
    access = Access()
    with sessions() as session, pytest.raises(DeployStateError):
        ensure_state_bucket(session, session.get(AwsEnvironment, identifier), access)
    assert access.calls == []


# --- save_foundation -------------------------------------------------------------------------
def test_the_foundation_values_are_saved_and_visible_to_the_next_adapter_environment(sessions):
    identifier = add(sessions, env_id="test", state_bucket=BUCKET)
    with sessions() as session:
        save_foundation(session, session.get(AwsEnvironment, identifier), FOUNDATION)
    saved = reload(sessions, identifier)
    assert {name: getattr(saved, name) for name in FOUNDATION} == FOUNDATION
    assert adapter_environment(saved).host == FOUNDATION["host"]


@pytest.mark.parametrize(
    "fields",
    [
        {k: v for k, v in FOUNDATION.items() if k != "host"},  # 항목이 빠졌다
        {**FOUNDATION, "state_bucket": BUCKET},  # 저장 대상이 아닌 항목이 섞였다
        {**FOUNDATION, "host": "bad host; rm -rf /"},
        {**FOUNDATION, "db_address": "evil.example.com"},  # RDS 도메인이 아니다
        {
            **FOUNDATION,
            "db_secret_arn": "arn:aws:secretsmanager:ap-northeast-2:111111111111:secret:rds!db-x",
        },  # 다른 계정
        {**FOUNDATION, "db_port": 80},
    ],
)
def test_invalid_foundation_values_are_rejected_and_nothing_is_changed(sessions, fields):
    identifier = add(sessions, env_id="test")
    with sessions() as session, pytest.raises(DeployStateError) as caught:
        save_foundation(session, session.get(AwsEnvironment, identifier), fields)
    assert caught.value.code == "foundation_invalid" and "rm -rf" not in caught.value.message
    assert all(getattr(reload(sessions, identifier), name) is None for name in FOUNDATION)


def test_foundation_values_are_not_saved_for_an_environment_that_is_not_connected(sessions):
    identifier = add(sessions, env_id="test", status="FAILED")
    with sessions() as session, pytest.raises(DeployStateError):
        save_foundation(session, session.get(AwsEnvironment, identifier), FOUNDATION)
    assert reload(sessions, identifier).host is None

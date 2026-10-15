from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from cryptography.fernet import Fernet
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import AwsEnvironment, Base, User, Workspace


@pytest.mark.parametrize(
    "changes",
    [
        {"aws_template_url": "http://bucket.s3.amazonaws.com/a.yaml"},
        {"aws_template_url": "https://s3.amazonaws.com.evil.invalid/a.yaml"},
        {"aws_template_url": "https://user:password@bucket.s3.amazonaws.com/a.yaml"},
        {"aws_template_url": "https://bucket.s3.amazonaws.com/a.yaml#fragment"},
        {"aws_service_role_arn": "arn:aws:iam::123456789012:user/name"},
        {"aws_regions": ("cn-north-1",)},
        {"aws_regions": ("not-a-region",)},
        {"aws_role_name": "bad/name"},
        {"aws_role_name": "a" * 40 + "{id}"},
    ],
)
def test_invalid_settings_rejected(changes):
    with pytest.raises(ValueError):
        Settings(demo=True, **changes)


def test_settings_from_environment_and_optional_aws(monkeypatch):
    monkeypatch.setenv("APP_DEMO", "false")
    monkeypatch.setenv("APP_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "APP_AWS_TEMPLATE_URL", "https://bucket.s3.ap-northeast-2.amazonaws.com/a.yaml"
    )
    monkeypatch.setenv("APP_AWS_SERVICE_ROLE_ARN", "arn:aws:iam::123456789012:role/service-server")
    monkeypatch.setenv("APP_AWS_REGIONS", "ap-northeast-2, us-east-1, ap-northeast-2")
    monkeypatch.setenv("APP_AWS_ROLE_NAME", "deploy-service-role-{id}")
    settings = Settings.from_env()
    assert settings.aws_configured and settings.aws_regions == ("ap-northeast-2", "us-east-1")
    assert settings.aws_role_name == "deploy-service-role-{id}"
    assert not Settings(demo=True).aws_configured


def test_upgrade_preserves_existing_data_and_matches_models(database_url):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "0004")
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            session.add(User(id="u1", github_id=123, login="existing", name="기존 사용자"))
            session.add(Workspace(id="w1", name="기존 워크스페이스"))
            session.commit()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        with Session(engine) as session:
            assert session.scalar(select(User.name)) == "기존 사용자"
            for identifier in ("a", "b"):
                session.add(
                    AwsEnvironment(
                        id=identifier,
                        workspace_id="w1",
                        created_by="u1",
                        request_id=identifier,
                        name="연결",
                        region="ap-northeast-2",
                        external_id=identifier * 64,
                        template_url="https://bucket.s3.amazonaws.com/a.yaml",
                        service_role_arn="arn:aws:iam::123456789012:role/source",
                        stack_name=identifier,
                        role_name="deploy-service-role",
                        created_at=1,
                        expires_at=2,
                    )
                )
            session.commit()  # Multiple pending rows may have a NULL role_arn.
        command.downgrade(config, "0004")
        assert "aws_environments" not in inspect(engine).get_table_names()
        with Session(engine) as session:
            assert session.scalar(select(User.name)) == "기존 사용자"
    finally:
        engine.dispose()


def test_submitted_role_migration_preserves_existing_connections(database_url):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "0005")
    engine = create_engine(database_url)
    role = "arn:aws:iam::123456789012:role/deploy-service-role"
    try:
        with Session(engine) as session:
            session.add(User(id="u1", github_id=123, login="existing", name="기존 사용자"))
            session.add(Workspace(id="w1", name="기존 워크스페이스"))
            session.commit()
        old = Table("aws_environments", MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            for identifier, status in (("a", "CONNECTED"), ("b", "FAILED")):
                connected = status == "CONNECTED"
                connection.execute(
                    old.insert().values(
                        id=identifier,
                        workspace_id="w1",
                        created_by="u1",
                        request_id=identifier,
                        name="기존 환경",
                        region="ap-northeast-2",
                        external_id=identifier * 64,
                        template_url="https://bucket.s3.amazonaws.com/a.yaml",
                        service_role_arn="arn:aws:iam::999999999999:role/service-server",
                        stack_name=identifier,
                        role_name="deploy-service-role",
                        status=status,
                        role_arn=role if connected else None,
                        aws_account_id="123456789012" if connected else None,
                        created_at=1,
                        expires_at=2,
                        verified_at=1 if connected else None,
                        error_code="" if connected else "service_credentials_unavailable",
                        verification_token="",
                        lease_until=0,
                    )
                )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        with Session(engine) as session:
            connected = session.get(AwsEnvironment, "a")
            failed = session.get(AwsEnvironment, "b")
            assert connected.submitted_role_arn == connected.role_arn == role
            assert connected.status == "CONNECTED" and connected.verified_at == 1
            assert failed.submitted_role_arn is None and failed.role_arn is None
            assert failed.status == "FAILED" and failed.external_id == "b" * 64
        command.downgrade(config, "0005")
        assert "submitted_role_arn" not in {
            column["name"] for column in inspect(engine).get_columns("aws_environments")
        }
        with engine.connect() as connection:
            assert connection.scalar(select(old.c.role_arn).where(old.c.id == "a")) == role
    finally:
        engine.dispose()

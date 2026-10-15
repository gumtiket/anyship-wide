from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import AwsEnvironment, DeployJob, Deployment, Project, User, Workspace

NEW_COLUMNS = {"env_id", "host", "db_address", "db_port", "db_secret_arn", "state_bucket"}


def environment(identifier, **extra):
    return AwsEnvironment(
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
        **extra,
    )


def job(identifier, request_id):
    return DeployJob(
        id=identifier,
        project_id="p1",
        request_id=request_id,
        runtime_id="r",
        action="deploy",
        set_name="aws-always-on",
        created_at=1,
    )


@pytest.fixture
def migrated(database_url):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "0007")
    engine = create_engine(database_url)
    with Session(engine) as session:  # 0007 시점의 기존 데이터
        session.add_all(
            [User(id="u1", github_id=1, login="u", name="기존"), Workspace(id="w1", name="w")]
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
        session.execute(
            AwsEnvironment.__table__.insert().values(
                id="old",
                workspace_id="w1",
                created_by="u1",
                request_id="old",
                name="기존 환경",
                region="ap-northeast-2",
                external_id="o" * 64,
                template_url="https://bucket.s3.amazonaws.com/a.yaml",
                service_role_arn="arn:aws:iam::123456789012:role/source",
                stack_name="old",
                role_name="deploy-service-role",
                status="CONNECTED",
                error_code="",
                created_at=1,
                expires_at=2,
                verification_token="",
                lease_until=0,
            )
        )
        session.commit()
    command.upgrade(config, "head")
    yield config, engine
    engine.dispose()


def test_existing_environments_get_empty_foundation_fields(migrated):
    _, engine = migrated
    with Session(engine) as session:
        old = session.get(AwsEnvironment, "old")
        assert old.name == "기존 환경" and old.status == "CONNECTED"
        assert all(getattr(old, name) is None for name in NEW_COLUMNS)


def test_foundation_fields_roundtrip_and_env_id_is_unique(migrated):
    _, engine = migrated
    with Session(engine) as session:
        session.add(
            environment(
                "a",
                env_id="test",
                host="43.201.158.8",
                db_address="db.example",
                db_port=5432,
                db_secret_arn="arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:x",
                state_bucket="anyship-tfstate-123456789012-ap-northeast-2-2b9b6060",
            )
        )
        session.commit()
        saved = session.get(AwsEnvironment, "a")
        assert (saved.env_id, saved.db_port, saved.host) == ("test", 5432, "43.201.158.8")
        session.add(environment("b", env_id="test"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add_all(
            [environment("c"), environment("d")]
        )  # env_id가 아직 없는 환경은 여럿 있어도 된다
        session.commit()


def test_deployment_and_job_constraints(migrated):
    _, engine = migrated
    with Session(engine) as session:
        session.add(Deployment(project_id="p1", aws_environment_id="old", set_name="aws-always-on"))
        session.add(job("j1", "r1"))
        session.commit()
        saved = session.get(Deployment, "p1")
        assert (saved.image_tag, saved.lease_until, saved.active_job_id) == ("", 0, None)
        assert session.get(DeployJob, "j1").status == "queued"
        session.add(job("j2", "r1"))  # 같은 프로젝트의 같은 요청 ID는 한 번만
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        with pytest.raises(
            IntegrityError
        ):  # 프로젝트당 배포 대상은 하나(세션의 중복 감지가 아니라 DB가 막는지 보려고 직접 넣는다)
            session.execute(
                Deployment.__table__.insert().values(
                    project_id="p1",
                    aws_environment_id="old",
                    set_name="onprem",
                    app_name="",
                    image_tag="",
                    url="",
                    lease_until=0,
                )
            )


def test_downgrade_removes_only_what_it_added(migrated):
    config, engine = migrated
    command.downgrade(config, "0007")
    inspector = inspect(engine)
    assert {"deployments", "deploy_jobs"}.isdisjoint(inspector.get_table_names())
    assert NEW_COLUMNS.isdisjoint(
        column["name"] for column in inspector.get_columns("aws_environments")
    )
    with Session(engine) as session:
        assert (
            session.execute(
                AwsEnvironment.__table__.select().with_only_columns(AwsEnvironment.__table__.c.name)
            ).scalar()
            == "기존 환경"
        )

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import (
    DeployJob,
    Deployment,
    OnpremEnvironment,
    OnpremRegistrationToken,
    Project,
    User,
    Workspace,
    database,
)
from tests.test_real_deployment_migration import environment


def test_migration_preserves_aws_targets_and_supports_nullable_transport(database_url):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "0009")
    engine, sessions = database(database_url)
    with sessions() as session:
        session.add_all(
            [User(id="u1", github_id=1, login="u", name="사용자"), Workspace(id="w1", name="w")]
        )
        session.flush()
        session.add_all(
            [
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
                ),
                environment("old"),
            ]
        )
        session.flush()
        session.execute(
            text(
                "INSERT INTO deployments (project_id,aws_environment_id,set_name,app_name,image_tag,url,lease_until) "
                "VALUES ('p1','old','aws-always-on','todo','abcdef0','https://old.example',0)"
            )
        )
        session.add(
            DeployJob(
                id="j1",
                project_id="p1",
                request_id="r1",
                runtime_id="r",
                action="deploy",
                set_name="aws-always-on",
                created_at=1,
            )
        )
        session.commit()
    command.upgrade(config, "head")
    with sessions() as session:
        target = session.get(Deployment, "p1")
        assert (target.aws_environment_id, target.onprem_environment_id, target.image_tag) == (
            "old",
            None,
            "abcdef0",
        )
        assert session.get(DeployJob, "j1").action == "deploy"
        row = OnpremEnvironment(
            id="n1",
            workspace_id="w1",
            created_by="u1",
            request_id="r1",
            name="내 서버",
            email="ops@example.com",
            env_id="envone",
            created_at=1,
        )
        session.add(row)
        session.flush()
        session.add(
            OnpremRegistrationToken(token_hash="h" * 64, environment_id="n1", expires_at=900)
        )
        session.commit()
        assert row.host is row.ssh_user is row.ssh_port is row.credential_ref is None
        assert row.connection_kind == "ssh"
        target.onprem_environment_id = row.id
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        target = session.get(Deployment, "p1")
        target.aws_environment_id, target.onprem_environment_id, target.set_name = (
            None,
            row.id,
            "onprem",
        )
        session.commit()
    with pytest.raises(RuntimeError, match="Remove on-premise"):
        command.downgrade(config, "0009")
    command.upgrade(
        config, "head"
    )  # 되돌리기는 0011을 먼저 되돌린 채 0010에서 거절되므로, 다음 확인을 위해 다시 올린다
    with sessions() as session:
        target = session.get(Deployment, "p1")
        target.aws_environment_id, target.onprem_environment_id, target.set_name = (
            "old",
            None,
            "aws-always-on",
        )
        session.commit()
    command.downgrade(config, "0009")
    assert "onprem_environments" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT image_tag FROM deployments")).scalar() == "abcdef0"
    engine.dispose()

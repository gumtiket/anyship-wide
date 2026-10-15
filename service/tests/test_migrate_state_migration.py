from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db import Deployment, Project, User, Workspace, database
from tests.test_real_deployment_migration import environment


def config(database_url):
    result = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    result.attributes["database_url"] = database_url
    return result


def seed(sessions):
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
        session.commit()


def test_existing_deployments_get_empty_previous_columns_and_survive_the_round_trip(database_url):
    cfg = config(database_url)
    command.upgrade(cfg, "0010")
    engine, sessions = database(database_url)
    seed(sessions)
    command.upgrade(cfg, "head")
    with sessions() as session:
        target = session.get(Deployment, "p1")
        assert (target.image_tag, target.url) == ("abcdef0", "https://old.example")
        assert (
            target.previous_kind,
            target.previous_environment_id,
            target.previous_app_name,
            target.previous_url,
        ) == ("", "", "", "")
    command.downgrade(cfg, "0010")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT image_tag FROM deployments")).scalar() == "abcdef0"
        assert "previous_app_name" not in [
            row[1] for row in connection.execute(text("PRAGMA table_info(deployments)"))
        ]
    engine.dispose()


def test_a_downgrade_that_would_forget_a_stopped_app_is_refused(database_url):
    cfg = config(database_url)
    command.upgrade(cfg, "0010")
    engine, sessions = database(database_url)
    seed(sessions)
    command.upgrade(cfg, "head")
    with sessions() as session:
        target = session.get(Deployment, "p1")
        target.previous_kind, target.previous_environment_id, target.previous_app_name = (
            "onprem",
            "n1",
            "todo",
        )
        session.commit()
    with pytest.raises(RuntimeError, match="stopped apps"):
        command.downgrade(cfg, "0010")
    with sessions() as session:  # 거절했으니 기록이 그대로다
        assert session.get(Deployment, "p1").previous_app_name == "todo"
    engine.dispose()

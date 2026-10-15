from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import AnalysisRun, AnalysisSlot, CodeChange, Project, User, Workspace


def test_analysis_migration_preserves_existing_rows_and_constraints(database_url):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "0008")
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            session.add_all(
                [User(id="u", github_id=1, login="u", name="기존"), Workspace(id="w", name="w")]
            )
            session.flush()
            session.add(
                Project(
                    id="p",
                    workspace_id="w",
                    repository_id=1,
                    installation_id=1,
                    full_name="o/r",
                    branch="main",
                    base_sha="a" * 40,
                    created_by="u",
                    created_at=1,
                )
            )
            session.flush()
            session.add(
                CodeChange(
                    id="old",
                    project_id="p",
                    base_sha="a" * 40,
                    base_tree="b" * 40,
                    branch="anyship/old",
                    content="old",
                )
            )
            session.commit()
        command.upgrade(config, "head")
        with Session(engine) as session:
            assert session.get(CodeChange, "old").content == "old"
            session.add(AnalysisSlot(project_id="p"))
            values = dict(
                project_id="p",
                request_id="request",
                runtime_id="runtime",
                repository_id=1,
                repository="o/r",
                base_branch="main",
                base_sha="a" * 40,
                provider="none",
                target_env="aws",
                branch="anyship/new",
                created_at=1,
            )
            session.add(AnalysisRun(id="new", **values))
            session.commit()
            assert session.get(AnalysisRun, "new").status == "queued"
            session.add(AnalysisRun(id="duplicate", **values))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        command.downgrade(config, "0008")
        assert {"analysis_runs", "analysis_slots"}.isdisjoint(inspect(engine).get_table_names())
        with Session(engine) as session:
            assert session.get(CodeChange, "old").content == "old"
            assert session.scalar(select(Project.id)) == "p"
    finally:
        engine.dispose()

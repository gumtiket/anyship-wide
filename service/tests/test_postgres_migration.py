from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import DataError

from app.db import Base, LoginSession, Membership, User, Workspace
from app.db_transfer import copy_sqlite_snapshot


def migrate(url):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")


@pytest.fixture
def transfer_databases(tmp_path, database_url):
    if not database_url.startswith("postgresql"):
        pytest.skip("Run scripts/local_postgres.py test for PostgreSQL migration checks.")
    source_url = f"sqlite:///{tmp_path / 'source.db'}"
    migrate(source_url)
    migrate(database_url)
    source, target = create_engine(source_url), create_engine(database_url)
    cipher = Fernet(Fernet.generate_key())
    encrypted = cipher.encrypt(b"fixture-github-token").decode()
    with source.begin() as connection:
        connection.execute(
            User.__table__.insert(),
            {"id": "u1", "github_id": 9000000000, "login": "anyship-test", "name": "테스트 사용자"},
        )
        connection.execute(Workspace.__table__.insert(), {"id": "w1", "name": "테스트 작업 공간"})
        connection.execute(
            Membership.__table__.insert(), {"user_id": "u1", "workspace_id": "w1", "role": "owner"}
        )
        connection.execute(
            LoginSession.__table__.insert(),
            {
                "id": "s1",
                "user_id": "u1",
                "workspace_id": "w1",
                "token_cipher": encrypted,
                "csrf": "test",
                "expires_at": 9000000000,
            },
        )
    try:
        yield source, target, cipher
    finally:
        source.dispose()
        target.dispose()


def test_migrations_and_transfer_preserve_unicode_large_ids_and_encrypted_tokens(
    transfer_databases,
):
    source, target, cipher = transfer_databases
    assert set(inspect(target).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
    counts = copy_sqlite_snapshot(source, target)
    assert counts["users"] == counts["workspaces"] == counts["login_sessions"] == 1
    with target.connect() as connection:
        row = connection.execute(select(User.__table__)).mappings().one()
        assert row["name"] == "테스트 사용자" and row["github_id"] == 9000000000
        encrypted = connection.scalar(select(LoginSession.token_cipher))
        assert cipher.decrypt(encrypted.encode()) == b"fixture-github-token"


def test_nonempty_target_is_never_overwritten(transfer_databases):
    source, target, _ = transfer_databases
    copy_sqlite_snapshot(source, target)
    with pytest.raises(ValueError, match="refusing to overwrite"):
        copy_sqlite_snapshot(source, target)
    with target.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(User)) == 1


def test_postgresql_constraint_failure_rolls_back_entire_transfer(transfer_databases):
    source, target, _ = transfer_databases
    with source.begin() as connection:
        connection.execute(LoginSession.__table__.update().values(csrf="x" * 65))
    with pytest.raises(DataError):
        copy_sqlite_snapshot(source, target)
    with target.connect() as connection:
        assert all(
            connection.scalar(select(func.count()).select_from(table)) == 0
            for table in Base.metadata.sorted_tables
        )

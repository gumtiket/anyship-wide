"""Back up SQLite, verify its PostgreSQL copy, then activate the new local database."""

import json
import shutil
import socket
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values, set_key
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url

from local_postgres import start

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "service"
sys.path.insert(0, str(SERVICE))
from app.db_transfer import copy_sqlite_snapshot  # noqa: E402  service 경로를 추가한 뒤에 가져온다


def main():
    profile = SERVICE / ".env.github.local"
    values = dotenv_values(profile)
    current = values.get("APP_DATABASE_URL")
    if not current or make_url(current).get_backend_name() != "sqlite":
        raise ValueError("The current local profile must use SQLite. No configuration was changed.")
    source_file = (SERVICE / make_url(current).database).resolve()
    if not source_file.is_file():
        raise ValueError("SQLite source file does not exist.")
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=1):
            raise ValueError(
                "Stop AnyShip on port 8000 before migrating, and keep all source DB writers stopped."
            )
    except OSError:
        pass
    target_url = start()
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    backup = ROOT / ".local" / "backups" / tag
    backup.mkdir(parents=True)
    snapshot = backup / "source.db"
    with (
        sqlite3.connect(source_file.as_uri() + "?mode=ro", uri=True) as reader,
        sqlite3.connect(snapshot) as writer,
    ):
        reader.backup(writer)
    shutil.copy2(profile, backup / "profile.env")
    migration = Config(str(SERVICE / "alembic.ini"))
    migration.attributes["database_url"] = target_url
    command.upgrade(migration, "head")
    source = create_engine(URL.create("sqlite", database=str(snapshot)))
    target = create_engine(target_url, hide_parameters=True)
    try:
        counts = copy_sqlite_snapshot(source, target)
    finally:
        source.dispose()
        target.dispose()
    if dotenv_values(profile).get("APP_DATABASE_URL") != current:
        raise ValueError(
            "Local profile changed during migration; copied data is preserved but activation was skipped."
        )
    set_key(profile, "APP_DATABASE_URL", target_url)
    (backup / "report.json").write_text(
        json.dumps(
            {
                "source_file": str(source_file),
                "counts": counts,
                "verified": True,
                "target": "127.0.0.1:55432/anyship",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps({"migration": "verified and activated", "counts": counts, "backup": str(backup)})
    )
    print(
        "Original SQLite and APP_TOKEN_KEY preserved. Start AnyShip with scripts/start-local.ps1."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # SQL errors can contain user data or credentials. Do not echo them.
        print(
            str(error)
            if isinstance(error, (ValueError, RuntimeError))
            else f"Migration failed ({type(error).__name__}); source database and backup are preserved."
        )
        raise SystemExit(1) from None

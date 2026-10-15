"""Copy a stopped service's SQLite snapshot into an empty, migrated PostgreSQL DB."""

import hashlib
import json

from sqlalchemy import func, inspect, select, text

from .db import Base


def fingerprint(rows):
    serialized = sorted(json.dumps(dict(row), sort_keys=True, ensure_ascii=False) for row in rows)
    return hashlib.sha256(json.dumps(serialized, ensure_ascii=False).encode()).digest()


def copy_sqlite_snapshot(source, target):
    if source.dialect.name != "sqlite" or target.dialect.name != "postgresql":
        raise ValueError("Transfer requires a SQLite source and a PostgreSQL target.")
    tables = Base.metadata.sorted_tables
    expected = {table.name for table in tables} | {"alembic_version"}
    for engine in (source, target):
        inspector = inspect(engine)
        if set(inspector.get_table_names()) != expected:
            raise ValueError("Database tables do not match the current migrations.")
        for table in tables:
            if {column["name"] for column in inspector.get_columns(table.name)} != set(
                table.columns.keys()
            ):
                raise ValueError(f"Column mismatch in {table.name}.")
    counts = {}
    with source.connect() as reader, target.begin() as writer:
        if (
            reader.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            != writer.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        ):
            raise ValueError("Source and target migration versions must match.")
        # Block concurrent writers for the entire copy and verification transaction.
        names = ", ".join(target.dialect.identifier_preparer.quote(table.name) for table in tables)
        writer.execute(text(f"LOCK TABLE {names} IN EXCLUSIVE MODE"))
        if any(writer.scalar(select(func.count()).select_from(table)) for table in tables):
            raise ValueError("Target contains application data; refusing to overwrite it.")
        for table in tables:
            rows = [dict(row) for row in reader.execute(select(table)).mappings()]
            if rows:
                writer.execute(table.insert(), rows)
            copied = list(writer.execute(select(table)).mappings())
            if len(rows) != len(copied) or fingerprint(rows) != fingerprint(copied):
                raise ValueError(f"Verification failed for {table.name}; transfer rolled back.")
            counts[table.name] = len(rows)
    return counts

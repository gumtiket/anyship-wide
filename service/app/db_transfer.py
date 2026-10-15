"""멈춘 서비스의 SQLite 스냅숏을 마이그레이션만 적용한 빈 PostgreSQL DB로 복사한다."""

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
        # 복사와 검증 트랜잭션 내내 다른 쓰기를 막는다.
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

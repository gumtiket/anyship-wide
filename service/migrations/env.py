import os
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

from app.config import DEFAULT_DATABASE_URL
from app.db import Base

load_dotenv(Path(__file__).resolve().parents[1] / ".env.github.local")
url = (
    context.config.attributes.get("database_url")
    or os.getenv("APP_DATABASE_URL")
    or DEFAULT_DATABASE_URL
)
if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()

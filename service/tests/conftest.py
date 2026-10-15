"""기본은 SQLite를 쓰고, 이름을 명시한 PostgreSQL 테스트 DB가 있으면 격리된 스키마를 쓴다."""

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

# 여러 테스트 모듈이 함께 쓰는 픽스처(web, real_web, aws_web, real)를 정의한 모듈.
pytest_plugins = ["tests.test_web", "tests.test_aws_onboarding", "tests.test_real_workflow"]


@pytest.fixture
def database_url(tmp_path):
    configured = os.environ.get("ANYSHIP_TEST_DATABASE_URL")
    if not configured:
        yield f"sqlite:///{tmp_path / 'test.db'}"
        return
    url = make_url(configured)
    if url.get_backend_name() != "postgresql" or not (url.database or "").endswith("_test"):
        raise ValueError(
            "ANYSHIP_TEST_DATABASE_URL must select a PostgreSQL database ending in _test."
        )
    schema = "anyship_test_" + uuid.uuid4().hex
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    try:
        yield url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
            hide_password=False
        )
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()

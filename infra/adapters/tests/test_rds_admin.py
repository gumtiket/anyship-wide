import subprocess
from pathlib import Path

import pytest
from specs import GENERATED

from anyship_adapters.compose import POSTGRES_IMAGE, render_stack
from anyship_adapters.models import AwsEnvironment
from anyship_adapters.rds_admin import (
    MASTER_USER,
    database_url,
    db_name,
    ensure_app_database,
    password_from_url,
)
from anyship_adapters.ssh import SshConnection, SshRunner

MASTER = "M4ster!Pass/word99"  # RDS가 만드는 비밀번호처럼 특수문자를 포함한다
APP_PASSWORD = "a1b2c3d4e5f6a7b8c9d0e1f2"
DB_ADDRESS = "anyship-test-db.abc123.ap-northeast-2.rds.amazonaws.com"


def env(**override):
    values = dict(
        env_id="test",
        role_arn="arn:aws:iam::223455088214:role/deploy-service-role",
        external_id="ext-id-0123456789abcdef",
        host="203.0.113.5",
        db_address=DB_ADDRESS,
    )
    return AwsEnvironment(**{**values, **override})


class Recorder:
    """ssh 실행기를 대신해 원격으로 보낼 명령과 표준입력을 기록한다."""

    def __init__(self, code=0, stderr=b""):
        self.calls, self.code, self.stderr = [], code, stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, self.code, stdout=b"", stderr=self.stderr)


def run(recorder=None, **override):
    recorder = recorder or Recorder()
    ssh = SshRunner(SshConnection("203.0.113.5", Path("/k")), runner=recorder)
    args = dict(master_password=MASTER, app_password=APP_PASSWORD) | override
    return ensure_app_database(ssh, env(), "todo-app", **args), recorder


# --- 이름과 주소 -------------------------------------------------------------------------------
def test_database_and_role_names_use_a_prefix_and_underscores():
    assert db_name("todo-app") == "app_todo_app"
    assert db_name("a-b-c") == "app_a_b_c"  # 'postgres' 같은 예약 이름과 겹치지 않는다
    assert db_name("postgres") == "app_postgres"


def test_the_longest_app_name_that_fits_an_identifier_is_accepted_and_one_more_is_not():
    assert len(db_name("a" * 59)) == 63
    with pytest.raises(ValueError):
        db_name("a" * 60)


@pytest.mark.parametrize(
    "name", ["Bad Name", "ab", "1abc", "x'; drop table users;--", "app_name", ""]
)
def test_invalid_app_names_never_reach_sql(name):
    with pytest.raises(ValueError):
        db_name(name)
    recorder = Recorder()
    ssh = SshRunner(SshConnection("203.0.113.5", Path("/k")), runner=recorder)
    with pytest.raises(ValueError):
        ensure_app_database(ssh, env(), name, master_password=MASTER, app_password=APP_PASSWORD)
    assert recorder.calls == []


def test_the_database_url_requires_ssl_and_is_accepted_by_the_compose_renderer():
    url = database_url(env(), "todo-app", APP_PASSWORD)
    assert (
        url
        == f"postgresql://app_todo_app:{APP_PASSWORD}@{DB_ADDRESS}:5432/app_todo_app?sslmode=require"
    )
    stack = render_stack(
        GENERATED, host="todo.test.aws.anyship.cloud", image_tag="abc1234", database_url=url
    )
    assert url in stack.app_env and url not in stack.compose_yaml


def test_the_app_password_can_be_recovered_from_a_previous_url_but_not_from_junk():
    url = database_url(env(), "todo-app", APP_PASSWORD)
    assert password_from_url(url) == APP_PASSWORD
    for junk in (None, "", "mysql://u:pw@h/db", "postgresql://u:short@h/db", "postgresql://u@h/db"):
        assert password_from_url(junk) is None


# --- 호스트에서 실행하는 명령 ---------------------------------------------------------------------
def test_passwords_travel_only_on_standard_input_never_on_the_command_line():
    result, recorder = run()
    cmd, kwargs = recorder.calls[0]
    remote = cmd[-1]  # ssh가 원격 셸에 넘기는 한 줄
    assert result.ok
    assert MASTER not in remote and APP_PASSWORD not in remote
    stdin = kwargs["input"].decode()
    assert stdin.split("\n", 1)[0] == MASTER  # 첫 줄은 마스터 비밀번호, 나머지는 SQL
    assert APP_PASSWORD in stdin


def test_the_container_gets_the_password_from_the_environment_not_from_an_argument():
    _, recorder = run()
    remote = recorder.calls[0][0][-1]
    assert "-e PGPASSWORD " in remote and "PGPASSWORD=" not in remote.replace(
        "export PGPASSWORD;", ""
    )
    assert (
        POSTGRES_IMAGE in remote and f"user={MASTER_USER}" not in remote
    )  # 사용자 이름도 인자로 전달된다
    assert DB_ADDRESS in remote and "sslmode=require" in remote


def test_the_sql_can_be_repeated_without_failing():
    _, recorder = run()
    sql = recorder.calls[0][1]["input"].decode().split("\n", 1)[1]
    assert (
        "IF NOT EXISTS (SELECT FROM pg_roles" in sql and "ALTER ROLE app_todo_app" in sql
    )  # 있으면 비밀번호만 맞춘다
    assert (
        "\\gexec" in sql and "WHERE NOT EXISTS (SELECT FROM pg_database" in sql
    )  # DB는 없을 때만 만든다
    statements = [
        line.strip() for line in sql.splitlines()
    ]  # 주석 처리된 줄이 아니라 실제 문장이어야 한다
    assert (
        "REVOKE ALL ON DATABASE app_todo_app FROM PUBLIC;" in statements
    )  # 다른 앱 계정은 접속하지 못한다
    assert "GRANT CONNECT ON DATABASE app_todo_app TO app_todo_app;" in statements


def test_a_failed_command_is_returned_as_a_result_not_raised():
    result, _ = run(Recorder(code=1, stderr=b"psql: connection timed out"))
    assert not result.ok and "timed out" in result.stderr


@pytest.mark.parametrize(
    "override",
    [
        {"app_password": "short"},
        {"app_password": "x" * 129},
        {"app_password": "abc'; DROP ROLE x;--abcdefgh"},
        {"app_password": "has space 0123456789ab"},
        {"master_password": ""},
        {"master_password": "line\nbreak"},
        {"master_password": "carriage\rreturn"},
    ],
)
def test_unsafe_passwords_are_refused_before_anything_is_sent(override):
    recorder = Recorder()
    with pytest.raises(ValueError):
        run(recorder, **override)
    assert recorder.calls == []


def test_a_missing_database_address_is_refused_before_anything_is_sent():
    recorder = Recorder()
    ssh = SshRunner(SshConnection("203.0.113.5", Path("/k")), runner=recorder)
    with pytest.raises(ValueError):
        ensure_app_database(
            ssh, env(db_address=None), "todo-app", master_password=MASTER, app_password=APP_PASSWORD
        )
    assert recorder.calls == []

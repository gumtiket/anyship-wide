"""공용 RDS 안에 앱 전용 DB와 계정을 만든다(aws-always-on 어댑터용).

서비스 서버는 사용자 VPC의 RDS에 직접 닿지 못하므로, 앱 호스트에서 `psql` 컨테이너를 한 번 실행한다
(`docker run --rm`). 호스트에는 AWS 자격 증명이 없어서, 비밀번호는 서비스 서버가 SSH **표준입력**으로만 보낸다.
명령줄(원격 `ps`에 보인다), 환경변수 파일, 디스크에는 비밀번호가 남지 않는다.

앱 하나 = 계정 하나 + DB 하나(이름은 같다). 몇 번을 실행해도 같은 결과가 되고(멱등), 이미 있는 계정은
비밀번호만 맞춘다. 앱 계정은 자기 DB에만 접속할 수 있고, 마스터 계정은 앱에 넘기지 않는다.

주의: psql 오류 출력에는 실패한 문장이 그대로 실릴 수 있어서 비밀번호가 섞일 수 있다.
호출하는 쪽은 두 비밀번호를 redact 대상에 등록한 뒤에 출력을 쓴다.
"""

import re

from .compose import POSTGRES_IMAGE
from .models import APP_NAME_PATTERN, AwsEnvironment
from .ssh import CommandResult, SshRunner

# infra/user-account/database.tf의 RDS 마스터 계정 이름과 같아야 한다.
MASTER_USER = "anyship_admin"
_APP = re.compile(APP_NAME_PATTERN)
_PASSWORD = re.compile(r"^[A-Za-z0-9]{16,128}$")  # SQL 문자열에 그대로 들어가므로 영숫자만 허용한다
_URL_PASSWORD = re.compile(r"^postgresql://[a-z][a-z0-9_]*:([A-Za-z0-9]{16,128})@")


def db_name(app: str) -> str:
    """앱 계정과 DB의 이름. `app_` 접두사로 `postgres` 같은 예약 이름과 겹치지 않게 한다."""
    name = "app_" + app.replace("-", "_")
    if not _APP.match(app) or len(name) > 63:  # PostgreSQL 식별자는 63자까지
        raise ValueError("invalid app name for database")
    return name


def database_url(env: AwsEnvironment, app: str, password: str) -> str:
    name = db_name(app)
    return f"postgresql://{name}:{password}@{env.db_address}:{env.db_port}/{name}?sslmode=require"


def password_from_url(url: str | None) -> str | None:
    """서버에 남은 이전 DATABASE_URL에서 앱 비밀번호를 꺼낸다. 재배포 때 바꾸지 않으려는 용도."""
    found = _URL_PASSWORD.match(url or "")
    return found.group(1) if found else None


def _sql(name: str, password: str) -> str:
    # name과 password는 위에서 형식을 검증했다(소문자·숫자·밑줄 / 영숫자). 따옴표가 들어갈 수 없다.
    return f"""DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{name}') THEN
    CREATE ROLE {name} LOGIN PASSWORD '{password}';
  ELSE
    ALTER ROLE {name} LOGIN PASSWORD '{password}';
  END IF;
END $$;
GRANT {name} TO CURRENT_USER;
SELECT 'CREATE DATABASE {name} OWNER {name}' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '{name}') \\gexec
REVOKE ALL ON DATABASE {name} FROM PUBLIC;
GRANT CONNECT ON DATABASE {name} TO {name};
"""


# 첫 줄에서 마스터 비밀번호를 읽고(표준입력), 나머지 입력은 psql이 SQL로 읽는다.
_REMOTE = (
    "IFS= read -r PGPASSWORD || exit 1; export PGPASSWORD; "
    'exec docker run --rm -i -e PGPASSWORD "$1" psql -v ON_ERROR_STOP=1 -X -q '
    '"host=$2 port=$3 user=$4 dbname=postgres sslmode=require connect_timeout=10"'
)


def ensure_app_database(
    ssh: SshRunner,
    env: AwsEnvironment,
    app: str,
    *,
    master_password: str,
    app_password: str,
    timeout: float = 180,
) -> CommandResult:
    """앱 전용 계정과 DB를 만들거나 비밀번호를 맞춘다. 실패는 예외가 아니라 결과로 알린다."""
    name = db_name(app)
    if (
        not _PASSWORD.match(app_password)
        or not master_password
        or "\n" in master_password
        or "\r" in master_password
    ):
        raise ValueError("invalid database password")
    if env.db_address is None:
        raise ValueError("database address is missing")
    stdin = f"{master_password}\n{_sql(name, app_password)}"
    return ssh.run(
        ["sh", "-c", _REMOTE, "sh", POSTGRES_IMAGE, env.db_address, str(env.db_port), MASTER_USER],
        input=stdin,
        timeout=timeout,
    )

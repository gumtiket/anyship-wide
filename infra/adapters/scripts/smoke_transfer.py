"""DB 이전의 PostgreSQL 명령을 **진짜 Postgres**로 시험하는 수동 시험(자동 테스트가 아니다). 서버나 AWS에 닿지 않는다.

docker가 있는 곳(서비스 서버)에서 실행한다. postgres:16-alpine 컨테이너 둘(원본, 대상)을 잠시 띄워서, 어댑터가 쓰는 **같은 명령과 SQL**로
행 수 세기, 크기 확인, `pg_dump`(커스텀 형식), `pg_restore`(`--clean --if-exists --single-transaction`)를 실행하고 결과를 직접 비교한다.

    python scripts/smoke_transfer.py

확인하는 것: 한국어와 많은 행이 그대로 옮겨지는지, 대상의 기존 테이블이 원본으로 바뀌는지(`--clean`), 시퀀스가 이어져서 복원 뒤 새 행을 넣어도
충돌하지 않는지, 크기 한도를 넘으면 거절하는지. 컨테이너는 포트를 열지 않고 끝나면 모두 지운다.

한계: 서버 사이를 SSH로 흘려보내는 부분(`SshRunner.popen`)과 RDS 쪽 명령(app.env를 읽어 DATABASE_URL을 넘기는 `docker run`)은 이 시험이 보지 못한다.
그 부분은 실제 서버 두 대로 따로 확인해야 한다.
"""

import subprocess
import sys
import time
import uuid

from anyship_adapters.compose_host import ComposeHost
from anyship_adapters.data_transfer import SIZE_SQL, ComposeDbEndpoint, transfer_database
from anyship_adapters.ssh import CommandResult

IMAGE = "postgres:16-alpine"
APP = "smoke"
ROWS = 20000
SCHEMA = """
CREATE TABLE users (id serial PRIMARY KEY, name text NOT NULL);
CREATE TABLE notes (id serial PRIMARY KEY, user_id int NOT NULL REFERENCES users(id), body text NOT NULL);
CREATE INDEX notes_user ON notes (user_id);
"""


class LocalDocker:
    """SshRunner 대신 쓴다. `docker compose ... exec -T db <도구>`를 `docker exec -i <컨테이너> <도구>`로 바꿔 실행한다."""

    def __init__(self, container: str):
        self.container = container

    def _mapped(self, args):
        if args[:2] == ["docker", "compose"] and "exec" in args:
            tool = args[args.index("db") + 1 :]
            return ["docker", "exec", "-i", self.container, *tool]
        return None

    def run(self, args, *, timeout=60, input=None, stdin=None):
        args = list(args)
        if args == ["true"] or args[:2] == ["test", "-f"]:
            return CommandResult(0)
        if args[:2] == ["docker", "compose"] and "ps" in args:
            return CommandResult(0, "db\n")  # 웹 컨테이너는 없다고 답한다(멈출 앱이 없다)
        if args[:2] == ["docker", "compose"] and "up" in args:
            return CommandResult(0)
        mapped = self._mapped(args)
        if mapped is None:
            return CommandResult(1, "", "이 시험이 흉내 내지 않는 명령: " + " ".join(args[:3]))
        feed = (
            {"stdin": stdin}
            if stdin is not None
            else {"input": input.encode() if isinstance(input, str) else input}
        )
        done = subprocess.run(mapped, capture_output=True, timeout=timeout, **feed)
        return CommandResult(
            done.returncode,
            done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"),
        )

    def popen(self, args):
        mapped = self._mapped(list(args))
        return subprocess.Popen(
            mapped, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
        )


def docker(*args, check=True, input=None):
    done = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        input=input,
        encoding="utf-8",
        errors="replace",
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"docker {args[0]} 실패: {done.stderr.strip()[-300:]}")
    return done


def psql(container: str, sql: str) -> str:
    return docker(
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        "app",
        "-d",
        "app",
        "-X",
        "-A",
        "-t",
        "-v",
        "ON_ERROR_STOP=1",
        input=sql,
    ).stdout.strip()


def start(name: str) -> None:
    docker(
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "POSTGRES_USER=app",
        "-e",
        "POSTGRES_DB=app",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        IMAGE,
    )
    for _ in range(60):
        if (
            docker("exec", name, "pg_isready", "-U", "app", "-d", "app", check=False).returncode
            == 0
        ):
            time.sleep(1)  # 초기화용 임시 서버가 내려가고 진짜 서버가 올라오는 짧은 틈을 지난다
            if (
                docker("exec", name, "pg_isready", "-U", "app", "-d", "app", check=False).returncode
                == 0
            ):
                return
        time.sleep(1)
    raise RuntimeError(f"{name}이(가) 준비되지 않았습니다")


results: list[tuple[bool, str]] = []


def check(ok: bool, what: str, extra: str = "") -> None:
    results.append((ok, what))
    print(f"[{'OK' if ok else 'FAIL'}] {what}{(' — ' + extra) if extra else ''}")


def main() -> int:
    tag = uuid.uuid4().hex[:8]
    source, target = f"anyship-xfer-src-{tag}", f"anyship-xfer-dst-{tag}"
    try:
        started = time.monotonic()
        start(source)
        start(target)
        print(f"컨테이너 준비 {time.monotonic() - started:.1f}초")

        psql(source, SCHEMA)
        psql(
            source,
            f"INSERT INTO users (name) VALUES ('김철수'), ('Alice'), ('日本語');"
            f"INSERT INTO notes (user_id, body) SELECT 1 + (g % 3), '메모 ' || g || ' 😀 ' || md5(g::text) "
            f"FROM generate_series(1, {ROWS}) AS g;",
        )
        # 대상: 이미 배포된 앱처럼 같은 스키마가 있고 다른 데이터가 조금 들어 있다(복원이 이 데이터를 원본으로 바꿔야 한다)
        psql(target, SCHEMA)
        psql(
            target,
            "INSERT INTO users (name) VALUES ('stale'); INSERT INTO notes (user_id, body) VALUES (1, 'stale');",
        )

        src = ComposeDbEndpoint(LocalDocker(source), ComposeHost(LocalDocker(source)), APP)
        dst = ComposeDbEndpoint(LocalDocker(target), ComposeHost(LocalDocker(target)), APP)
        digest = "SELECT md5(string_agg(id || ':' || body, '|' ORDER BY id)) FROM notes;"

        check(
            src.query(SIZE_SQL).stdout.strip().isdigit(),
            "크기 확인 SQL이 숫자를 돌려준다",
            src.query(SIZE_SQL).stdout.strip() + "바이트",
        )

        events = []
        began = time.monotonic()
        result = transfer_database(src, dst, events.append)
        elapsed = time.monotonic() - began
        check(
            result.ok,
            "transfer_database가 성공한다",
            f"{elapsed:.1f}초 · {result.details}" if result.ok else str(result.error),
        )
        if result.ok:
            check(
                result.details["rows"] == ROWS + 3 and result.details["tables"] == 2,
                "행 수 검증이 원본과 같은 값을 센다",
                f"행 {result.details['rows']}",
            )
            check(
                psql(target, "SELECT count(*) FROM notes;") == str(ROWS),
                "대상의 notes 행 수가 원본과 같다",
            )
            check(
                psql(target, "SELECT count(*) FROM notes WHERE body = 'stale';") == "0",
                "대상의 기존 데이터가 원본으로 바뀐다(--clean)",
            )
            check(
                psql(source, digest) == psql(target, digest),
                "모든 행의 내용(한국어, 이모지 포함)이 똑같다",
            )
            check(
                psql(target, "SELECT name FROM users ORDER BY id LIMIT 1;") == "김철수",
                "한국어가 깨지지 않는다",
            )
            try:
                psql(target, "INSERT INTO notes (user_id, body) VALUES (1, '복원 뒤 새 행');")
                check(True, "시퀀스가 이어져서 복원 뒤 새 행을 넣어도 충돌하지 않는다")
            except RuntimeError as exc:
                check(False, "시퀀스가 이어져서 복원 뒤 새 행을 넣어도 충돌하지 않는다", str(exc))
            check(
                psql(target, "SELECT count(*) FROM pg_indexes WHERE indexname = 'notes_user';")
                == "1",
                "인덱스도 옮겨진다",
            )
            owners = psql(
                target, "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public';"
            )
            check(owners == "app", "소유자는 대상의 계정으로 만들어진다(--no-owner)", owners)

        refused = transfer_database(src, dst, lambda event: None, max_bytes=1)
        check(
            not refused.ok and refused.error.code == "db_too_large", "크기 한도를 넘으면 거절한다"
        )
        check(
            psql(target, "SELECT count(*) FROM notes;") == str(ROWS + 1),
            "거절한 뒤에도 대상은 그대로다",
        )
    except Exception as exc:  # 시험 환경 문제(docker 없음 등)
        print(f"[FAIL] 시험을 끝내지 못했습니다: {exc}")
        results.append((False, "시험 진행"))
    finally:
        for name in (source, target):
            docker("rm", "-f", "-v", name, check=False)
    failed = [what for ok, what in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

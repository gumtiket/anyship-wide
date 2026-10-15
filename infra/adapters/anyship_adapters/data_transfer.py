"""앱 데이터(DB)를 한 환경에서 다른 환경으로 옮긴다(PostgreSQL에서 PostgreSQL로, 예: 온프레미스 서버에서 AWS로).

S3 같은 중간 저장소를 쓰지 않는다. 원본 서버의 `pg_dump` 출력을 서비스 서버가 **그대로 중계**해서 대상 서버의 `pg_restore` 입력으로 흘려보낸다
(`docker save | docker load`로 이미지를 보내는 것과 같은 방식). 파일을 만들지 않고 메모리에 모으지도 않으므로 서비스 서버의 디스크와 메모리는
크기에 상관없이 쓰지 않지만, 서비스 서버의 네트워크를 지나가므로 아주 큰 DB는 한도(`max_bytes`)로 거절한다.

순서: 확인(두 서버, 앱, DB 크기) → 쓰기 중지(웹 컨테이너만 멈춘다. DB는 켜 둔다) → 덤프를 복원으로 흘려보내기 → 행 수 검증 → 대상 앱 시작.
  * 실패하면 멈춘 컨테이너를 모두 되돌린다. 복원은 한 트랜잭션이라 실패하면 대상 DB는 그대로다.
  * 성공하면 **원본 앱은 멈춘 채로 둔다.** 사용자가 새 주소에서 확인한 뒤 원본을 지우거나, 문제가 있으면 원본을 다시 켠다(`source_stopped`).
  * 대상에는 같은 앱이 먼저 배포되어 있어야 한다(빈 DB로). 복원은 `--clean --if-exists`라서 대상의 기존 테이블을 원본의 것으로 바꾼다.

범위: DB만 옮긴다. 컨테이너 볼륨의 파일, 앱이 만든 비밀 값(SECRET_KEY 등, 환경마다 따로 생성)은 옮기지 않는다.
비밀번호는 명령줄에 쓰지 않는다. 온프레미스의 DB는 컨테이너 안에서 소켓으로 접속하고, RDS는 호스트의 `app.env`를 읽어서 `DATABASE_URL`을
환경변수로만 컨테이너에 넘긴다(`rds_admin`과 같은 원칙).
"""

import re

from .base import LogFn
from .compose import DB_NAME, DB_USER, POSTGRES_IMAGE
from .compose_adapter import _ssh_error
from .compose_host import ComposeHost
from .models import APP_NAME_PATTERN, AdapterError, LogEvent, TransferResult
from .redact import redact_text
from .ssh import CommandResult, SshRunner

APPS_DIR = "/opt/apps"
DEFAULT_MAX_BYTES = 1024**3  # 1 GiB
_APP = re.compile(APP_NAME_PATTERN)
_COUNT_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\|([0-9]+)$")

_DUMP_FLAGS = "--format=custom --no-owner --no-privileges"
_RESTORE_FLAGS = (
    "--no-owner --no-privileges --clean --if-exists --single-transaction --exit-on-error"
)
_PSQL_FLAGS = "-X -A -t -v ON_ERROR_STOP=1"

SIZE_SQL = "SELECT pg_database_size(current_database());\n"
# 테이블마다 count(*)를 한 번에 센다(통계 추정치가 아니라 실제 행 수). `이름|행 수` 줄을 돌려준다.
COUNT_SQL = (
    "SELECT table_name || '|' || (xpath('/row/c/text()', query_to_xml("
    "format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text "
    "FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1;\n"
)


def _err(code: str, message: str, hint: str | None = None, retryable: bool = False) -> AdapterError:
    return AdapterError(code=code, message=message, hint=hint, retryable=retryable)


class TransferInputError(Exception):
    """데이터를 옮길 수 없는 환경이라서 접속 방법을 만들지 못할 때(예: 공용 기반이 없음). 호출하는 쪽이 결과의 오류로 바꾼다."""

    def __init__(self, error: AdapterError):
        super().__init__(error.message)
        self.error = error


class DbEndpoint:
    """한 환경에서 앱의 DB에 닿는 방법. 어느 서버의 어떤 DB인지는 서브클래스가 안다."""

    def __init__(self, ssh: SshRunner, host: ComposeHost, app: str):
        if not _APP.match(app):
            raise ValueError("invalid app name")
        self.ssh, self.host, self.app = ssh, host, app

    def dump_args(self) -> list[str]:
        raise NotImplementedError

    def restore_args(self) -> list[str]:
        raise NotImplementedError

    def psql_args(self) -> list[str]:
        raise NotImplementedError

    def query(self, sql: str, *, timeout: float = 120) -> CommandResult:
        """SQL을 표준 입력으로 보내 실행한다(명령줄에 SQL을 싣지 않는다)."""
        return self.ssh.run(self.psql_args(), input=sql, timeout=timeout)


class ComposeDbEndpoint(DbEndpoint):
    """온프레미스: 앱 전용 `db` 컨테이너. 컨테이너 안에서 소켓으로 접속해서 비밀번호가 필요 없다."""

    def _exec(self, tool: str, flags: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-directory",
            f"{APPS_DIR}/{self.app}",
            "exec",
            "-T",
            "db",
            tool,
            "-U",
            DB_USER,
            "-d",
            DB_NAME,
            *flags.split(),
        ]

    def dump_args(self) -> list[str]:
        return self._exec("pg_dump", _DUMP_FLAGS)

    def restore_args(self) -> list[str]:
        return self._exec("pg_restore", _RESTORE_FLAGS)

    def psql_args(self) -> list[str]:
        return self._exec("psql", _PSQL_FLAGS)


# 호스트의 app.env를 읽어(`NAME='값'` 줄뿐이라 셸이 그대로 읽는다) DATABASE_URL만 환경변수로 컨테이너에 넘긴다.
# 비밀번호는 명령줄에도 로그에도 남지 않는다. app.env의 값은 렌더링할 때 작은따옴표와 줄바꿈이 없도록 검증됐다.
_RDS_RUN = (
    'env_file=$1; image=$2; shift 2; . "$env_file"; export DATABASE_URL; '
    'exec docker run --rm -i -e DATABASE_URL "$image" "$@"'
)


class RdsDbEndpoint(DbEndpoint):
    """aws-always-on: 공용 RDS 안의 앱 전용 DB. RDS는 호스트에서만 닿으므로 호스트에서 postgres 컨테이너를 한 번 실행한다."""

    def _run(self, inner: str) -> list[str]:
        return [
            "sh",
            "-c",
            _RDS_RUN,
            "sh",
            f"{APPS_DIR}/{self.app}/app.env",
            POSTGRES_IMAGE,
            "sh",
            "-c",
            inner,
        ]

    def dump_args(self) -> list[str]:
        return self._run(f'exec pg_dump --dbname "$DATABASE_URL" {_DUMP_FLAGS}')

    def restore_args(self) -> list[str]:
        return self._run(f'exec pg_restore --dbname "$DATABASE_URL" {_RESTORE_FLAGS}')

    def psql_args(self) -> list[str]:
        return self._run(f'exec psql {_PSQL_FLAGS} "$DATABASE_URL"')


def _tail(text: str) -> str:
    return redact_text(text[-500:])


def _read_counts(endpoint: DbEndpoint) -> dict[str, int] | None:
    done = endpoint.query(COUNT_SQL)
    if not done.ok:
        return None
    counts: dict[str, int] = {}
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        found = _COUNT_LINE.match(line.strip())
        if not found:
            return None
        counts[found.group(1)] = int(found.group(2))
    return counts


class _Transfer:
    TOTAL = 5

    def __init__(
        self, source: DbEndpoint, target: DbEndpoint, log: LogFn, max_bytes: int, timeout: float
    ):
        self.source, self.target, self.log, self.max_bytes, self.timeout = (
            source,
            target,
            log,
            max_bytes,
            timeout,
        )
        self.stopped: list[DbEndpoint] = []  # 이 작업이 멈춘 앱. 실패하면 되돌린다

    def step(self, number: int, name: str, message: str) -> None:
        self.log(LogEvent(step=number, total=self.TOTAL, name=name, message=message))

    def fail(self, error: AdapterError, **details) -> TransferResult:
        self.log(LogEvent(level="error", message=error.message))
        return TransferResult(ok=False, error=error, details=details)

    def resume(self, *endpoints: DbEndpoint) -> None:
        for endpoint in endpoints:
            if endpoint in self.stopped:
                try:
                    endpoint.host.up(endpoint.app)  # 이미 켜져 있어도 안전하다
                finally:
                    self.stopped.remove(endpoint)

    def resume_all(self) -> None:
        for endpoint in list(self.stopped):
            try:
                self.resume(endpoint)
            except Exception:  # 하나가 실패해도 나머지는 되돌린다
                pass

    def run(self) -> TransferResult:
        source, target = self.source, self.target
        self.step(1, "준비 확인", "두 환경에 접속해 앱과 DB를 확인하는 중")
        for side, endpoint in (("원본", source), ("대상", target)):
            reached = endpoint.ssh.run(["true"])
            if not reached.ok:
                error = _ssh_error(reached)
                return self.fail(
                    _err(error.code, f"{side} 환경: {error.message}", error.hint, error.retryable)
                )
            if not endpoint.host.exists(endpoint.app):
                return self.fail(
                    _err(
                        "app_not_found",
                        f"{side} 환경에 이 앱이 배포되어 있지 않습니다.",
                        hint="대상 환경에 같은 앱을 먼저 배포해 주세요(빈 DB로 시작합니다).",
                    )
                )
        sized = source.query(SIZE_SQL)
        if not sized.ok or not sized.stdout.strip().isdigit():
            return self.fail(
                _err(
                    "source_db_unavailable",
                    "원본 DB에 접속하지 못했습니다.",
                    hint="원본 앱의 DB가 실행 중인지 확인해 주세요.",
                    retryable=True,
                ),
                stderr=_tail(sized.stderr),
            )
        size = int(sized.stdout.strip())
        if size > self.max_bytes:
            return self.fail(
                _err(
                    "db_too_large",
                    f"DB가 너무 커서 옮길 수 없습니다({size}바이트, 한도 {self.max_bytes}바이트).",
                    hint="이 방식은 서비스 서버를 거치는 작은 DB용입니다.",
                ),
                bytes=size,
            )
        if not target.query("SELECT 1;\n").ok:
            return self.fail(
                _err(
                    "target_db_unavailable",
                    "대상 DB에 접속하지 못했습니다.",
                    hint="대상 환경에 앱이 배포되어 DB가 준비됐는지 확인해 주세요.",
                    retryable=True,
                )
            )

        self.step(2, "쓰기 중지", "원본과 대상의 웹 컨테이너를 멈추는 중(DB는 켜 둡니다)")
        for side, endpoint in (("원본", source), ("대상", target)):
            if "web" in endpoint.host.running_services(endpoint.app):
                stopped = endpoint.host.stop_service(endpoint.app)
                if not stopped.ok:
                    self.resume_all()
                    return self.fail(
                        _err("quiesce_failed", f"{side} 앱을 멈추지 못했습니다.", retryable=True),
                        stderr=_tail(stopped.stderr),
                    )
                self.stopped.append(endpoint)

        self.step(3, "데이터 복사", f"원본 DB({size}바이트)를 대상 DB로 옮기는 중")
        dump = source.ssh.popen(source.dump_args())
        try:
            restored = target.ssh.run(
                target.restore_args(), stdin=dump.stdout, timeout=self.timeout
            )
        finally:
            dump.stdout.close()
        if restored.timed_out:  # 복원이 멈추면 덤프도 멈춘다
            dump.kill()
        dump_errors = dump.stderr.read().decode("utf-8", errors="replace") if dump.stderr else ""
        dump_code = dump.wait()
        if dump_code != 0:
            self.resume_all()
            return self.fail(
                _err("dump_failed", "원본 DB를 내보내지 못했습니다.", retryable=True),
                stderr=_tail(dump_errors),
            )
        if not restored.ok:
            self.resume_all()
            if restored.timed_out:
                return self.fail(
                    _err(
                        "restore_timeout",
                        "대상 DB로 옮기는 데 시간이 너무 오래 걸렸습니다.",
                        retryable=True,
                    )
                )
            return self.fail(
                _err(
                    "restore_failed",
                    "대상 DB에 데이터를 넣지 못했습니다.",
                    hint="대상 DB는 변경되지 않았습니다(한 트랜잭션). 원본 앱은 다시 시작했습니다.",
                    retryable=True,
                ),
                stderr=_tail(restored.stderr),
            )

        self.step(4, "검증", "원본과 대상의 테이블별 행 수를 비교하는 중")
        before, after = _read_counts(source), _read_counts(target)
        if before is None or after is None:
            self.resume(source)
            return self.fail(_err("verify_failed", "행 수를 확인하지 못했습니다.", retryable=True))
        if before != after:
            self.resume(source)  # 대상은 의심스러운 데이터라 멈춘 채로 둔다
            differing = sorted(
                name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
            )
            return self.fail(
                _err(
                    "verify_mismatch",
                    "옮긴 데이터의 행 수가 원본과 다릅니다.",
                    hint="대상 앱은 멈춘 상태로 두었고 원본 앱은 다시 시작했습니다.",
                    retryable=True,
                ),
                tables=differing,
            )

        self.step(5, "마무리", "대상 앱을 시작하는 중(원본 앱은 멈춘 채로 둡니다)")
        started = target.host.up(target.app)  # 멈췄던 대상 앱을 시작한다(켜져 있었다면 그대로 둔다)
        if target in self.stopped:
            self.stopped.remove(target)
        if not started.ok:
            # 데이터는 옮겨졌지만 대상이 서지 못한다. 양쪽이 모두 멈춘 채 두지 않고 원본이 다시 서게 한다(그동안 쓰기는 없었다).
            self.resume(source)
            return self.fail(
                _err(
                    "target_start_failed",
                    "대상 앱을 시작하지 못했습니다.",
                    hint="원본 앱을 다시 시작했습니다. 대상 환경에서 앱이 시작되지 않는 원인을 확인해 주세요.",
                    retryable=True,
                ),
                stderr=_tail(started.stderr),
            )
        was_stopped = source in self.stopped
        if was_stopped:
            self.stopped.remove(source)  # 성공하면 원본은 일부러 되돌리지 않는다
        return TransferResult(
            ok=True,
            details={
                "bytes": size,
                "tables": len(after),
                "rows": sum(after.values()),
                "source_stopped": was_stopped,
            },
        )


def inspect_source(
    source: DbEndpoint, log: LogFn, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> TransferResult:
    """옮기기 전에 원본만 미리 확인한다(접속, 앱이 있는지, DB 크기가 한도 안인지). 아무것도 멈추거나 바꾸지 않는다.

    대상 환경을 새로 배포하기 전에 부르면, 옮길 수 없는 DB 때문에 오래 걸리는 배포를 헛되이 하는 일을 막는다.
    성공하면 `details["bytes"]`에 DB 크기가 담긴다."""
    log(LogEvent(step=1, total=1, name="원본 확인", message="원본 서버, 앱, DB 크기를 확인하는 중"))

    def refuse(error: AdapterError, **details) -> TransferResult:
        log(LogEvent(level="error", message=error.message))
        return TransferResult(ok=False, error=error, details=details)

    reached = source.ssh.run(["true"])
    if not reached.ok:
        error = _ssh_error(reached)
        return refuse(_err(error.code, f"원본 환경: {error.message}", error.hint, error.retryable))
    if not source.host.exists(source.app):
        return refuse(_err("app_not_found", "원본 환경에 이 앱이 배포되어 있지 않습니다."))
    sized = source.query(SIZE_SQL)
    if not sized.ok or not sized.stdout.strip().isdigit():
        return refuse(
            _err(
                "source_db_unavailable",
                "원본 DB에 접속하지 못했습니다.",
                hint="원본 앱의 DB가 실행 중인지 확인해 주세요.",
                retryable=True,
            ),
            stderr=_tail(sized.stderr),
        )
    size = int(sized.stdout.strip())
    if size > max_bytes:
        return refuse(
            _err(
                "db_too_large",
                f"DB가 너무 커서 옮길 수 없습니다({size}바이트, 한도 {max_bytes}바이트).",
                hint="이 방식은 서비스 서버를 거치는 작은 DB용입니다.",
            ),
            bytes=size,
        )
    return TransferResult(ok=True, details={"bytes": size})


def transfer_database(
    source: DbEndpoint,
    target: DbEndpoint,
    log: LogFn,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = 1500,
) -> TransferResult:
    """원본의 DB를 대상으로 옮긴다. 실패는 예외가 아니라 결과로 알리고, 멈춘 앱은 되돌린다."""
    work = _Transfer(source, target, log, max_bytes, timeout)
    try:
        return work.run()
    except BaseException:
        work.resume_all()  # 예상하지 못한 오류에서도 멈춘 앱을 되돌린다
        raise

import json
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from ai.build_context import BuildContext
from ai.gate.models import GateStep
from ai.gate.runner import CURL_IMAGE, POSTGRES_IMAGE, ContainerRunner, RunnerError
from ai.models import GateReport
from ai.spec.models import DeploySpec
from ai.stages import LogFn, Stage

FAILURE_PATTERNS = (
    "Read-only file system",
    "unable to open database file",
    "PermissionError",
    "OSError: [Errno 30]",
)


def safe_log(text: str, sensitive: list[str]) -> str:
    for value in sorted(sensitive, key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text, flags=re.I)
    text = re.sub(r"(?<!\d)\d{12}(?!\d)", "[ACCOUNT_ID]", text)
    return "\n".join(text.splitlines()[-200:])[-20000:]


def tree_tag(context: BuildContext) -> str:
    return "bronze-ai-gate:gate-" + context.tree_digest()[:24]


def run_gate(
    ctx: BuildContext,
    spec: DeploySpec,
    runner: ContainerRunner,
    log: LogFn,
    *,
    original_ctx: BuildContext | None = None,
    timeout_s: int = 40,
    sleep: Callable[[float], None] = time.sleep,
) -> GateReport:
    """One sample-only run. No user data migration or runtime code-repair loop."""
    if not ctx.verify_sample() or original_ctx is not None and not original_ctx.verify_sample():
        return GateReport(
            status="skipped", reason="해시로 확인한 자체 샘플의 임시 트리만 실행합니다."
        )
    report = GateReport(
        status="failed",
        runner="docker" if runner.real else "fake",
        reason="샘플 시작·임시 Postgres CRUD 검증",
        scope="sample_startup_and_postgres_crud",
    )
    run_id = "bronze-gate-" + secrets.token_hex(6)
    network = run_id + "-net"
    password = secrets.token_urlsafe(24)
    database = f"postgresql://gate:{password}@{run_id}-db:5432/gate"
    sensitive = [password, database]
    containers = []
    images = []
    network_started = False
    sequence = 0
    current = report.transformed
    app_name = run_id + "-app"

    def start(image: str, suffix: str, env: dict[str, str], command: list[str]) -> str:
        nonlocal sequence
        sequence += 1
        name = run_id + "-" + suffix
        containers.append(name)
        runner.run(image=image, name=name, network=network, env=env, command=command)
        return name

    def one_shot(image: str, suffix: str, command: list[str], env: dict[str, str] | None = None):
        name = start(image, suffix, env or {}, command)
        try:
            return runner.wait(name, timeout=min(timeout_s, 30))
        finally:
            runner.remove(name)
            containers.remove(name)

    def step(name: str, action: Callable[[], str]) -> str:
        started = time.monotonic()
        log(Stage.BUILDING if "build" in name else Stage.VALIDATING, name)
        try:
            text = action()
        except (RunnerError, ValueError, OSError) as error:
            current.steps.append(
                GateStep(
                    name=name,
                    status="failed",
                    duration_s=time.monotonic() - started,
                    log=safe_log(str(error), sensitive),
                )
            )
            raise
        current.steps.append(
            GateStep(
                name=name,
                status="passed",
                duration_s=time.monotonic() - started,
                log=safe_log(text, sensitive),
            )
        )
        return text

    def build(context: BuildContext) -> str:
        tag = tree_tag(context)
        images.append(tag)
        current.image_tag = tag
        result = runner.build(Path(context.root), tag)
        if result.code:
            raise RunnerError("gate_build_failed\n" + result.output)
        return result.output

    def ready() -> str:
        deadline = time.monotonic() + timeout_s
        last = ""
        for attempt in range(timeout_s):
            result = one_shot(
                POSTGRES_IMAGE,
                f"ready-{attempt}",
                ["pg_isready", "-h", run_id + "-db", "-U", "gate", "-d", "gate"],
            )
            last = result.output
            if result.code == 0:
                return last
            if time.monotonic() >= deadline:
                break
            sleep(1)
        raise RunnerError("postgres_readiness_failed\n" + last)

    def request(suffix: str, path: str, *, method: str = "GET", payload: dict | None = None) -> str:
        command = [
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--max-time",
            "3",
            "--request",
            method,
        ]
        if payload is not None:
            command += ["--header", "Content-Type: application/json", "--data", json.dumps(payload)]
        command += [f"http://{app_name}:8080{path}"]
        result = one_shot(CURL_IMAGE, suffix, command)
        if result.code:
            raise RunnerError("http_request_failed\n" + result.output)
        return result.output

    def healthy() -> str:
        deadline = time.monotonic() + timeout_s
        last = ""
        for attempt in range(timeout_s):
            try:
                text = request(f"health-{attempt}", spec.healthcheck)
                payload = json.loads(text)
                if not isinstance(payload, dict) or payload.get("status") != "ok":
                    raise ValueError("health_payload_invalid")
                return text
            except (RunnerError, ValueError) as error:
                last = str(error)
                if time.monotonic() >= deadline:
                    break
                sleep(1)
        raise RunnerError("app_health_failed\n" + last)

    def crud() -> str:
        created = json.loads(
            request("create", "/todos", method="POST", payload={"title": "gate-validation"})
        )
        todo_id = created["id"]
        if created.get("title") != "gate-validation" or type(todo_id) is not int or todo_id < 1:
            raise ValueError("crud_create_invalid")
        rows = json.loads(request("read", "/todos"))
        if not any(
            row.get("id") == todo_id and row.get("title") == "gate-validation" for row in rows
        ):
            raise ValueError("crud_read_invalid")
        # Restart the app against the same temporary DB; verifies persistence outside its FS.
        runner.remove(app_name)
        containers.remove(app_name)
        start(current.image_tag, "app", app_env, [])
        healthy()
        rows = json.loads(request("read-after-restart", "/todos"))
        if not any(
            row.get("id") == todo_id and row.get("title") == "gate-validation" for row in rows
        ):
            raise ValueError("postgres_persistence_invalid")
        updated = json.loads(
            request(
                "update",
                f"/todos/{todo_id}",
                method="PUT",
                payload={"title": "gate-validation-updated", "done": True},
            )
        )
        if updated.get("id") != todo_id or not updated.get("done"):
            raise ValueError("crud_update_invalid")
        rows = json.loads(request("verify-updated", "/todos"))
        if not any(
            row.get("id") == todo_id
            and row.get("title") == "gate-validation-updated"
            and row.get("done") is True
            for row in rows
        ):
            raise ValueError("crud_updated_read_invalid")
        request("delete", f"/todos/{todo_id}", method="DELETE")
        rows = json.loads(request("verify-deleted", "/todos"))
        if any(row.get("id") == todo_id for row in rows):
            raise ValueError("crud_deleted_read_invalid")
        return "임시 Postgres 저장·조회·앱 재기동 후 조회·수정·삭제 확인"

    app_env = {
        "PORT": "8080",
        "LOG_LEVEL": "INFO",
        "SECRET_KEY": "dummy-secret-do-not-use",
        "DATABASE_URL": database,
    }
    try:
        step("preflight", lambda: runner.preflight() or "Docker/helpers 준비 확인")
        step("build", lambda: build(ctx))
        step("network", lambda: runner.network_create(network) or "internal network")
        network_started = True
        step(
            "postgres",
            lambda: (
                start(
                    POSTGRES_IMAGE,
                    "db",
                    {
                        "POSTGRES_USER": "gate",
                        "POSTGRES_DB": "gate",
                        "POSTGRES_PASSWORD": password,
                        "PGDATA": "/tmp/pgdata",
                    },
                    ["postgres"],
                )
                or ""
            ),
        )
        step("postgres_ready", ready)
        if spec.release is None or spec.release.migrate != "python -m app.migrate":
            raise ValueError("trusted_sample_migration_required")

        def migrate() -> str:
            # C uses compose run --rm web: a separate container with the same
            # app image/environment, after app startup and before healthcheck.
            result = one_shot(
                current.image_tag, "migrate", ["sh", "-c", "python -m app.migrate"], app_env
            )
            if result.code:
                raise RunnerError("migration_failed\n" + result.output)
            return result.output

        step("app_start", lambda: start(current.image_tag, "app", app_env, []))
        step("migrate", migrate)
        step("healthcheck", healthy)
        step("postgres_crud", crud)
        current.logs = safe_log(runner.logs(app_name), sensitive)
        current.status = "passed"
        if original_ctx is not None:
            current = report.original
            step("original_build", lambda: build(original_ctx))
            original_name = step(
                "original_start",
                lambda: start(
                    current.image_tag,
                    "original",
                    {"PORT": "8080", "LOG_LEVEL": "INFO", "SECRET_KEY": "dummy-secret-do-not-use"},
                    [],
                ),
            )
            result = runner.wait(original_name, timeout=min(timeout_s, 30))
            current.logs = safe_log(result.output, sensitive)
            current.matched_patterns = [
                pattern for pattern in FAILURE_PATTERNS if pattern in current.logs
            ]
            current.status = "failed" if current.matched_patterns else "passed"
            if not current.matched_patterns:
                raise RunnerError("original_expected_failure_not_observed")
        report.status = "passed" if runner.real else "skipped"
        if not runner.real:
            report.reason = "FakeRunner 순서/보안 검증만 완료. 실제 컨테이너 검증 아님."
    except (RunnerError, ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        current.status = "failed"
        report.reason = safe_log(str(error), sensitive)
        if app_name in containers:
            try:
                report.transformed.logs = safe_log(runner.logs(app_name), sensitive)
            except (RunnerError, ValueError):
                pass
        db_name = run_id + "-db"
        if db_name in containers:
            try:
                report.transformed.logs += "\nPostgres:\n" + safe_log(
                    runner.logs(db_name), sensitive
                )
            except (RunnerError, ValueError):
                pass
    finally:
        for name in reversed(containers):
            try:
                runner.remove(name)
            except (RunnerError, ValueError) as error:
                report.transformed.cleanup_errors.append(safe_log(str(error), sensitive))
        if network_started:
            try:
                runner.network_remove(network)
            except (RunnerError, ValueError) as error:
                report.transformed.cleanup_errors.append(safe_log(str(error), sensitive))
        for image in reversed(images):
            try:
                runner.image_remove(image)
            except (RunnerError, ValueError) as error:
                report.transformed.cleanup_errors.append(safe_log(str(error), sensitive))
        if report.transformed.cleanup_errors:
            report.status = "failed"
            report.reason = "gate_cleanup_failed"
    log(
        Stage.FAILED if report.status == "failed" else Stage.VALIDATING,
        f"게이트 결과: {report.status}; 현재 PR 승인 가능 표시는 false입니다.",
    )
    return report

"""aws-always-on 어댑터: 사용자 AWS 계정의 공용 EC2 호스트에 Compose로 앱을 배포한다.

온프레미스 어댑터와 같은 부품(ComposeAdapter, ComposeHost, render_stack, SshRunner, redact)을 쓰고, 다음만 다르다.
  * 접속 정보: 사용자가 입력한 서버 주소 대신, 공용 기반(infra/user-account)의 출력이 환경 정보로 들어온다.
  * 접근 권한: 사용자 계정의 역할을 AssumeRole한다(aws_access).
  * 앱 DB: 앱마다 컨테이너가 아니라 공용 RDS 안의 전용 DB와 계정(rds_admin).
  * 주소 규칙: `<앱>.<환경ID>.aws.<도메인>`. DNS 레코드는 미리 만들어 둔 환경을 전제로 한다.

status, rollback, destroy는 공통 클래스(ComposeAdapter)의 것을 쓰고, 호스트 정보가 있는지만 먼저 확인한다.
앱 destroy는 컨테이너와 파일만 지우고 앱 DB는 남긴다. 온프레미스 전용 기능(DNS 자동화 등)은 갖지 않는다.
"""

from collections.abc import Callable
from pathlib import Path
from secrets import token_hex

from .aws_access import AwsAccess, AwsAccessError
from .base import LogFn
from .compose import render_stack
from .compose_adapter import ComposeAdapter, _err, _ssh_error, _step
from .compose_host import ComposeHost, wait_healthy
from .data_transfer import RdsDbEndpoint, TransferInputError
from .models import (
    AdapterError,
    AwsEnvironment,
    CheckResult,
    DeployResult,
    DestroyResult,
    LogEvent,
    Secrets,
    Spec,
    StatusResult,
)
from .rds_admin import database_url, db_name, ensure_app_database, password_from_url
from .redact import make_safe_log, redact_model, redact_text
from .sets import AWS_ALWAYS_ON, SetName
from .spec import SpecError, parse_spec
from .ssh import CommandResult, SshRunner

# 기반이 만들어졌는지 보려고 확인하는 필드와, 읽기만 하는 작업(상태, 되돌리기, 삭제)에 필요한 필드.
_FOUNDATION = ("host", "db_address", "db_secret_arn")
_HOST_ONLY = ("host",)


def foundation_error(
    env: AwsEnvironment, needed: tuple[str, ...] = _FOUNDATION
) -> AdapterError | None:
    missing = [name for name in needed if getattr(env, name) is None]
    if not missing:
        return None
    return _err(
        "foundation_missing",
        "사용자 계정의 공용 기반 정보가 없습니다: " + ", ".join(missing),
        hint="공용 기반(VPC, 앱 호스트, RDS)을 먼저 만들고, 그 출력값을 환경 정보에 넣어 주세요.",
    )


class AwsAlwaysOnAdapter(ComposeAdapter):
    def __init__(
        self,
        key_path: Path,
        *,
        base_domain: str = "anyship.cloud",
        verify_tls: bool = True,
        connect: Callable[[AwsEnvironment], tuple[SshRunner, ComposeHost]] | None = None,
        healthy=wait_healthy,
        access: AwsAccess | None = None,
        dns_managed: bool = False,
    ):
        super().__init__(
            key_path,
            base_domain=base_domain,
            verify_tls=verify_tls,
            connect=connect,
            healthy=healthy,
        )
        self._access = access or AwsAccess()
        # `Deployer`가 DNS 레코드를 자동으로 맞추는 환경이면 True(서비스의 APP_DEPLOY_DNS=on). 어댑터는 DNS를 만들지 않으므로 로그 문구만 바꾼다.
        self._dns_managed = dns_managed

    def _address(self, env: AwsEnvironment, app: str) -> str:
        return f"{app}.{env.env_id}.aws.{self._domain}"

    def data_endpoint(self, env: AwsEnvironment, app: str) -> RdsDbEndpoint:
        """앱의 DB(공용 RDS 안의 앱 전용 DB)에 닿는 방법. 호스트가 있어야 한다. 데이터 이전이 쓴다."""
        error = foundation_error(env, _HOST_ONLY)
        if error:
            raise TransferInputError(error)
        ssh, host = self._connect(env)
        return RdsDbEndpoint(ssh, host, app)

    def check(self, env: AwsEnvironment, log: LogFn) -> CheckResult:
        total = 5
        _step(log, 1, total, "기반 확인", "공용 기반의 출력값이 있는지 확인하는 중")
        missing = foundation_error(env)
        if missing:
            return CheckResult(ok=False, error=missing)
        _step(log, 2, total, "역할 확인", "사용자 계정의 역할을 맡을 수 있는지 확인하는 중")
        try:
            account = self._access.check_role(env)
        except AwsAccessError as exc:
            return CheckResult(ok=False, error=exc.error)
        ssh, _ = self._connect(env)
        _step(log, 3, total, "접속 확인", "앱 호스트에 SSH로 접속하는 중")
        reached = ssh.run(["true"])
        if not reached.ok:
            return CheckResult(ok=False, error=_ssh_error(reached))
        _step(log, 4, total, "Docker 확인", "Docker와 Compose가 있는지 확인하는 중")
        if not ssh.run(["docker", "compose", "version"]).ok:
            return CheckResult(
                ok=False,
                error=_err(
                    "docker_missing",
                    "앱 호스트에 Docker 또는 Docker Compose가 없습니다.",
                    hint="호스트가 처음 부팅하는 중일 수 있습니다. 몇 분 뒤 다시 시도해 주세요.",
                    retryable=True,
                ),
            )
        _step(log, 5, total, "프록시 확인", "HTTPS를 처리하는 Traefik이 실행 중인지 확인하는 중")
        proxy = ssh.run(
            [
                "docker",
                "ps",
                "--filter",
                "label=com.docker.compose.project=traefik",
                "--filter",
                "label=com.docker.compose.service=traefik",
                "--filter",
                "status=running",
                "--format",
                "{{.Names}}",
            ]
        )
        if not proxy.stdout.strip():
            return CheckResult(
                ok=False,
                error=_err(
                    "proxy_not_ready",
                    "앱 호스트에서 Traefik이 실행 중이지 않습니다.",
                    hint="호스트가 처음 부팅하는 중일 수 있습니다. 계속되면 /opt/apps/traefik에서 Traefik을 시작해 주세요.",
                    retryable=True,
                ),
            )
        return CheckResult(ok=True, details={"account_id": account, "host": env.host})

    def deploy(
        self,
        env: AwsEnvironment,
        spec: Spec,
        image_tag: str,
        secrets: Secrets,
        log: LogFn,
        *,
        set_name: SetName,
    ) -> DeployResult:
        if set_name != AWS_ALWAYS_ON:
            return DeployResult(
                ok=False,
                error=_err(
                    "set_not_supported", f"이 어댑터에서는 '{set_name}' 세트를 사용할 수 없습니다."
                ),
            )
        missing = foundation_error(env)
        if missing:
            return DeployResult(ok=False, error=missing)
        try:
            parsed = parse_spec(spec)
            db_name(parsed.app)  # 앱 이름이 DB 식별자 한도(63자)에 맞는지
        except SpecError as exc:
            return DeployResult(ok=False, error=exc.error)
        except ValueError:
            return DeployResult(
                ok=False,
                error=_err(
                    "invalid_spec",
                    "앱 이름이 너무 길어 DB 이름을 만들 수 없습니다.",
                    hint="앱 이름을 59자 이하로 줄여 주세요.",
                ),
            )

        total, app = 8, parsed.app
        known = dict(
            secrets
        )  # 걸러낼 비밀. 아래에서 읽거나 만든 비밀번호도 여기에 더한다(같은 딕셔너리를 참조).
        safe = make_safe_log(log, known)

        def fail(error: AdapterError, result: CommandResult | None = None) -> DeployResult:
            error = redact_model(error, known)
            safe(LogEvent(level="error", message=error.message))
            details = (
                {"stderr": redact_text(result.stderr[-500:], known)}
                if result and result.stderr
                else {}
            )
            return DeployResult(ok=False, error=error, image_tag=image_tag, details=details)

        address = self._address(env, app)
        try:  # 서버 없이 알 수 있는 입력 오류(태그, 비밀 누락 등)부터 거른다.
            render_stack(spec, host=address, image_tag=image_tag, secrets=secrets)
        except SpecError as exc:
            return fail(exc.error)

        ssh, host = self._connect(env)
        _step(safe, 1, total, "환경 점검", "앱 호스트에 접속하고 이전 설정을 읽는 중")
        reached = ssh.run(["true"])
        if not reached.ok:
            return fail(_ssh_error(reached), reached)
        previous = host.read_previous_env(app)
        known.update(previous)

        url = None
        if parsed.postgres:
            _step(
                safe,
                2,
                total,
                "DB 비밀번호 준비",
                "사용자 계정의 비밀 저장소에서 DB 관리자 비밀번호를 읽는 중",
            )
            try:
                master = self._access.read_master_password(env)
            except AwsAccessError as exc:
                return fail(exc.error)
            app_password = password_from_url(previous.get("DATABASE_URL")) or token_hex(
                24
            )  # 재배포는 기존 값 유지
            known.update({"RDS_MASTER_PASSWORD": master, "APP_DB_PASSWORD": app_password})
            _step(
                safe,
                3,
                total,
                "앱 DB 준비",
                f"공용 RDS에 앱 전용 DB({db_name(app)})와 계정을 맞추는 중",
            )
            created = ensure_app_database(
                ssh, env, app, master_password=master, app_password=app_password
            )
            if not created.ok:
                return fail(
                    _err(
                        "db_setup_failed",
                        "앱 전용 DB를 준비하지 못했습니다.",
                        hint="호스트에서 RDS(5432)에 닿는지, 공용 RDS가 사용 가능한 상태인지 확인해 주세요.",
                        retryable=True,
                    ),
                    created,
                )
            url = database_url(env, app, app_password)
        else:
            _step(safe, 2, total, "DB 비밀번호 준비", "명세에 postgres가 없어 건너뜁니다")
            _step(safe, 3, total, "앱 DB 준비", "명세에 postgres가 없어 건너뜁니다")
        try:
            stack = render_stack(
                spec,
                host=address,
                image_tag=image_tag,
                secrets=secrets,
                previous_env=previous,
                database_url=url,
            )
        except SpecError as exc:
            return fail(exc.error)
        for warning in stack.warnings:
            safe(LogEvent(level="warn", message=warning))

        _step(safe, 4, total, "이미지 전달", f"이미지 {app}:{image_tag}를 호스트로 보내는 중")
        sent = host.load_image(f"{app}:{image_tag}")
        if not sent.ok:
            return fail(
                _err(
                    "image_transfer_failed",
                    "이미지를 호스트로 전달하지 못했습니다.",
                    hint="서비스 서버에 이 이미지가 있는지, 호스트의 디스크 여유가 있는지 확인해 주세요.",
                    retryable=True,
                ),
                sent,
            )
        _step(safe, 5, total, "파일 쓰기", "앱 설정 파일을 호스트에 쓰는 중")
        written = host.write_stack(app, stack)
        if not written.ok:
            return fail(
                _err(
                    "server_write_failed",
                    "호스트에 설정 파일을 쓰지 못했습니다.",
                    hint="/opt/apps 디렉터리의 쓰기 권한과 디스크 여유를 확인해 주세요.",
                ),
                written,
            )
        _step(safe, 6, total, "앱 시작", "컨테이너를 시작하는 중")
        started = host.up(app)
        if not started.ok:
            return fail(
                _err(
                    "container_start_failed",
                    "앱 컨테이너를 시작하지 못했습니다.",
                    hint="호스트에서 docker compose logs로 시작 오류를 확인해 주세요.",
                    retryable=True,
                ),
                started,
            )
        if parsed.migrate:
            _step(safe, 7, total, "마이그레이션", "데이터베이스 마이그레이션을 실행하는 중")
            migrated = host.migrate(app, parsed.migrate)
            if not migrated.ok:
                return fail(
                    _err(
                        "migration_failed",
                        "데이터베이스 마이그레이션이 실패했습니다.",
                        hint="마이그레이션 명령과 데이터베이스 연결 설정을 확인해 주세요.",
                    ),
                    migrated,
                )
        else:
            _step(safe, 7, total, "마이그레이션", "명세에 마이그레이션이 없어 건너뜁니다")
        # 어댑터는 DNS를 만들지도 확인하지도 않는다(그래서 단계로 세지 않는다). DNS 자동 관리가 켜져 있으면 배포기가 기반을 확인한 직후 이미 맞췄다.
        if self._dns_managed:
            safe(
                LogEvent(
                    message=f"{address}의 DNS 레코드는 서비스가 환경 단위로 자동으로 맞춥니다."
                )
            )
        else:
            safe(
                LogEvent(
                    level="warn",
                    message=f"{address}가 이 호스트({env.host})를 가리키는 DNS 레코드는 "
                    "미리 만들어 두어야 합니다(자동 생성 안 함).",
                )
            )
        _step(safe, 8, total, "헬스체크", "공개 주소로 앱이 응답하는지 확인하는 중")
        healthy, status = self._healthy(
            f"https://{address}{parsed.healthcheck}", verify_tls=self._verify_tls
        )
        if not healthy:
            return fail(
                _err(
                    "healthcheck_failed",
                    f"앱이 시작됐지만 헬스체크를 통과하지 못했습니다(마지막 응답: {status}).",
                    hint="호스트에서 앱 컨테이너가 계속 다시 시작되지 않는지(docker compose logs web), 앱이 PORT 환경변수의 포트에서 요청을 받고 "
                    "/healthz가 200을 반환하는지, Traefik이 인증서를 받았는지(docker logs traefik-traefik-1), "
                    "*.<환경ID>.aws 도메인의 DNS 레코드가 호스트 IP를 가리키는지 확인해 주세요.",
                    retryable=True,
                )
            )
        return DeployResult(
            ok=True,
            url=f"https://{address}",
            image_tag=image_tag,
            details={
                "warnings": list(stack.warnings),
                "generated": list(stack.generated),
                "database": db_name(app) if parsed.postgres else None,
            },
        )

    # -- 읽기와 정리: 호스트 정보만 있으면 되고, 동작은 온프레미스와 같다 -----------------------------
    def status(self, env: AwsEnvironment, app: str) -> StatusResult:
        error = foundation_error(env, _HOST_ONLY)
        return StatusResult(ok=False, error=error) if error else super().status(env, app)

    def rollback(self, env: AwsEnvironment, app: str, image_tag: str, log: LogFn) -> DeployResult:
        error = foundation_error(env, _HOST_ONLY)
        return (
            DeployResult(ok=False, error=error)
            if error
            else super().rollback(env, app, image_tag, log)
        )

    def destroy(self, env: AwsEnvironment, app: str, log: LogFn) -> DestroyResult:
        error = foundation_error(env, _HOST_ONLY)
        return DestroyResult(ok=False, error=error) if error else super().destroy(env, app, log)

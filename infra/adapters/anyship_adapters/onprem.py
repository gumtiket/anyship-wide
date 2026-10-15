"""온프레미스 어댑터: 사용자의 서버에 SSH로 접속해 Compose로 앱을 배포한다.

공용 부품을 조립한다: spec(검증), compose(파일), ssh와 compose_host(서버 조작), redact(비밀).
이 환경에만 해당하는 것은 앱 주소 규칙(`<앱>.<환경ID>.onprem.<도메인>`)과 앱 전용 DB 컨테이너다.

Adapter 인터페이스의 다섯 함수를 모두 구현한다. DNS 레코드 자동 생성과 Traefik 설치는
아직 없다(Traefik은 미리 설치해 둔 것을 쓴다). DNS 레코드는 `dns`를 주면 deploy가 환경 단위로
맞추고, 주지 않으면 미리 만들어 둔 레코드를 쓴다.
"""

import ipaddress
from collections.abc import Callable
from pathlib import Path

from .base import LogFn
from .compose import render_stack
from .compose_adapter import ComposeAdapter, _err, _ssh_error, _step
from .compose_host import ComposeHost, wait_healthy
from .data_transfer import ComposeDbEndpoint
from .dns import DnsError, WildcardRecords
from .models import (
    AdapterError,
    CheckResult,
    DeployResult,
    DestroyResult,
    LogEvent,
    OnpremEnvironment,
    Secrets,
    Spec,
)
from .redact import make_safe_log, redact_model, redact_text
from .sets import ONPREM, SetName
from .spec import SpecError, parse_spec
from .ssh import CommandResult, SshRunner


class OnpremAdapter(ComposeAdapter):
    def __init__(
        self,
        key_path: Path,
        *,
        base_domain: str = "anyship.cloud",
        verify_tls: bool = True,
        connect: Callable[[OnpremEnvironment], tuple[SshRunner, ComposeHost]] | None = None,
        healthy=wait_healthy,
        dns: WildcardRecords | None = None,
    ):
        super().__init__(
            key_path,
            base_domain=base_domain,
            verify_tls=verify_tls,
            connect=connect,
            healthy=healthy,
        )
        self._dns = dns  # None이면 DNS를 건드리지 않는다(레코드를 미리 만들어 둔 환경용)

    def _address(self, env: OnpremEnvironment, app: str) -> str:
        return f"{app}.{env.env_id}.onprem.{self._domain}"

    def data_endpoint(self, env: OnpremEnvironment, app: str) -> ComposeDbEndpoint:
        """앱의 DB에 닿는 방법(앱 전용 db 컨테이너). 데이터 이전이 쓴다."""
        ssh, host = self._connect(env)
        return ComposeDbEndpoint(ssh, host, app)

    def check(self, env: OnpremEnvironment, log: LogFn) -> CheckResult:
        ssh, _ = self._connect(env)
        _step(log, 1, 4, "접속 확인", "서버에 SSH로 접속하는 중")
        reached = ssh.run(["true"])
        if not reached.ok:
            return CheckResult(ok=False, error=_ssh_error(reached))
        _step(log, 2, 4, "Docker 확인", "Docker와 Compose가 있는지 확인하는 중")
        if not ssh.run(["docker", "compose", "version"]).ok:
            return CheckResult(
                ok=False,
                error=_err(
                    "docker_missing",
                    "서버에 Docker 또는 Docker Compose가 없습니다.",
                    hint="서버 준비 스크립트(setup.sh)를 실행해 주세요.",
                ),
            )
        _step(log, 3, 4, "프록시 확인", "HTTPS를 처리하는 Traefik이 실행 중인지 확인하는 중")
        # 컨테이너 이름(traefik-traefik-1 등)은 Compose 설정에 따라 달라지므로, 이름이 아니라
        # Compose가 붙이는 라벨(프로젝트 traefik의 서비스 traefik)로 실행 중인 것을 찾는다.
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
                    "서버에서 Traefik이 실행 중이지 않습니다.",
                    hint="/opt/apps/traefik에서 Traefik을 먼저 시작해 주세요.",
                ),
            )
        _step(log, 4, 4, "공개 IP 확인", "서버의 공개 IP를 조회하는 중")
        public_ip = _public_ip(ssh)
        if public_ip is None:
            return CheckResult(ok=False, error=_public_ip_error())
        return CheckResult(ok=True, details={"public_ip": public_ip})

    def deploy(
        self,
        env: OnpremEnvironment,
        spec: Spec,
        image_tag: str,
        secrets: Secrets,
        log: LogFn,
        *,
        set_name: SetName,
    ) -> DeployResult:
        if set_name != ONPREM:
            return DeployResult(
                ok=False,
                error=_err(
                    "set_not_supported",
                    f"온프레미스 환경에서는 '{set_name}' 세트를 사용할 수 없습니다.",
                ),
            )
        try:
            parsed = parse_spec(spec)
        except SpecError as exc:
            return DeployResult(ok=False, error=exc.error)

        total, app = 7, parsed.app
        known = dict(
            secrets
        )  # 걸러낼 비밀. 서버에서 읽은 값도 아래에서 여기에 더한다(같은 딕셔너리를 참조).
        safe = make_safe_log(log, known)

        def fail(error: AdapterError, result: CommandResult | None = None) -> DeployResult:
            error = redact_model(
                error, known
            )  # 오류 문구에 비밀이 섞여 들어와도 결과로 나가지 않게 한다
            safe(LogEvent(level="error", message=error.message))
            details = (
                {"stderr": redact_text(result.stderr[-500:], known)}
                if result and result.stderr
                else {}
            )
            return DeployResult(ok=False, error=error, image_tag=image_tag, details=details)

        address = f"{app}.{env.env_id}.onprem.{self._domain}"
        try:  # 서버에 접속하기 전에, 서버 없이 알 수 있는 입력 오류(태그, 비밀 누락 등)부터 거른다.
            render_stack(spec, host=address, image_tag=image_tag, secrets=secrets)
        except SpecError as exc:
            return fail(exc.error)

        ssh, host = self._connect(env)
        _step(safe, 1, total, "환경 점검", "서버에 접속하고 이전 설정을 읽는 중")
        reached = ssh.run(["true"])
        if not reached.ok:
            return fail(_ssh_error(reached), reached)
        previous = host.read_previous_env(app)
        known.update(previous)
        try:  # 이번에는 서버의 이전 비밀을 반영해서 다시 변환한다(서버의 값이 잘못됐을 수 있다).
            stack = render_stack(
                spec, host=address, image_tag=image_tag, secrets=secrets, previous_env=previous
            )
        except SpecError as exc:
            return fail(exc.error)
        for warning in stack.warnings:
            safe(LogEvent(level="warn", message=warning))

        if self._dns is None:
            _step(
                safe,
                2,
                total,
                "DNS 준비",
                "DNS 자동 설정이 꺼져 있어 건너뜁니다(레코드를 미리 만들어 둔 환경)",
            )
        else:
            _step(
                safe,
                2,
                total,
                "DNS 준비",
                "서버의 공개 IP로 환경의 DNS 레코드를 맞추는 중(새로 만들면 약 30초)",
            )
            public_ip = _public_ip(ssh)
            if public_ip is None:
                return fail(_public_ip_error())
            try:
                changed = self._dns.ensure(env.env_id, public_ip)
            except DnsError as exc:
                return fail(exc.error)
            safe(
                LogEvent(
                    message="DNS 레코드를 새로 반영했습니다."
                    if changed
                    else "DNS 레코드가 이미 맞습니다."
                )
            )

        _step(safe, 3, total, "이미지 전달", f"이미지 {app}:{image_tag}를 서버로 보내는 중")
        sent = host.load_image(f"{app}:{image_tag}")
        if not sent.ok:
            return fail(
                _err(
                    "image_transfer_failed",
                    "이미지를 서버로 전달하지 못했습니다.",
                    hint="서비스 서버에 이 이미지가 있는지, 서버의 디스크 여유가 있는지 확인해 주세요.",
                    retryable=True,
                ),
                sent,
            )
        _step(safe, 4, total, "파일 쓰기", "앱 설정 파일을 서버에 쓰는 중")
        written = host.write_stack(app, stack)
        if not written.ok:
            return fail(
                _err(
                    "server_write_failed",
                    "서버에 설정 파일을 쓰지 못했습니다.",
                    hint="/opt/apps 디렉터리의 쓰기 권한과 디스크 여유를 확인해 주세요.",
                ),
                written,
            )
        _step(safe, 5, total, "앱 시작", "컨테이너를 시작하는 중")
        started = host.up(app)
        if not started.ok:
            return fail(
                _err(
                    "container_start_failed",
                    "앱 컨테이너를 시작하지 못했습니다.",
                    hint="서버에서 docker compose logs로 시작 오류를 확인해 주세요.",
                    retryable=True,
                ),
                started,
            )
        if parsed.migrate:
            _step(safe, 6, total, "마이그레이션", "데이터베이스 마이그레이션을 실행하는 중")
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
            _step(safe, 6, total, "마이그레이션", "명세에 마이그레이션이 없어 건너뜁니다")
        _step(safe, 7, total, "헬스체크", "공개 주소로 앱이 응답하는지 확인하는 중")
        healthy, status = self._healthy(
            f"https://{address}{parsed.healthcheck}", verify_tls=self._verify_tls
        )
        if not healthy:
            return fail(
                _err(
                    "healthcheck_failed",
                    f"앱이 시작됐지만 헬스체크를 통과하지 못했습니다(마지막 응답: {status}).",
                    hint="앱이 PORT 환경변수의 포트에서 요청을 받고 /healthz가 200을 반환하는지 확인해 주세요.",
                    retryable=True,
                )
            )
        return DeployResult(
            ok=True,
            url=f"https://{address}",
            image_tag=image_tag,
            details={"warnings": list(stack.warnings), "generated": list(stack.generated)},
        )

    def remove_environment_dns(self, env: OnpremEnvironment, log: LogFn) -> DestroyResult:
        """환경의 DNS 레코드를 지운다. Adapter 인터페이스에는 없고, 환경 전체를 지울 때 서비스가 따로 부른다.

        레코드는 환경의 모든 앱이 함께 쓰므로 `destroy(app)`는 지우지 않는다. 호출하는 쪽이 이 환경에
        남은 앱이 없을 때만 불러야 한다(어댑터는 남은 앱을 세지 않는다). 서버에 접속하지 않는다.
        """
        if self._dns is None:
            _step(log, 1, 1, "DNS 제거", "DNS 자동 설정이 꺼져 있어 건너뜁니다")
            return DestroyResult(ok=True)
        _step(log, 1, 1, "DNS 제거", "환경의 DNS 레코드를 지우는 중(약 30초)")
        try:
            removed = self._dns.remove(env.env_id)
        except DnsError as exc:
            error = redact_model(exc.error)
            log(LogEvent(level="error", message=error.message))
            return DestroyResult(ok=False, error=error)
        log(
            LogEvent(
                message="DNS 레코드를 지웠습니다." if removed else "지울 DNS 레코드가 없습니다."
            )
        )
        return DestroyResult(ok=True)


def _public_ip(ssh: SshRunner) -> str | None:
    """서버가 인터넷에서 보이는 공개 IPv4. 알 수 없으면 None."""
    found = ssh.run(["curl", "-fsS", "--max-time", "10", "https://checkip.amazonaws.com"])
    text = found.stdout.strip()
    return text if found.ok and _is_public_ipv4(text) else None


def _public_ip_error() -> AdapterError:
    return _err(
        "public_ip_unknown",
        "서버의 공개 IP를 확인할 수 없습니다.",
        hint="서버가 인터넷으로 나갈 수 있어야 하고, 공인 IP가 있어야 합니다.",
        retryable=True,
    )


def _is_public_ipv4(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_global

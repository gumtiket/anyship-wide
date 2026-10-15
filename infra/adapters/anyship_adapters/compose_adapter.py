"""SSH로 서버에 Compose 앱을 배포하는 어댑터들의 공통 부분(온프레미스와 aws-always-on이 함께 쓴다).

이 클래스가 하는 일: 서버 접속 준비, 그리고 배포된 앱을 살펴보고(status), 되돌리고(rollback), 지우는(destroy) 일.
환경마다 다른 것은 서브클래스가 정한다: 앱 주소(`_address`), 점검(check), 배포(deploy).
destroy는 컨테이너, 앱 스택의 볼륨, 서버의 앱 디렉터리를 지운다. 외부 DB(RDS)의 데이터는 건드리지 않는다.
"""

import re
from collections.abc import Callable
from pathlib import Path

from .base import LogFn
from .compose_host import ComposeHost, wait_healthy
from .models import (
    APP_NAME_PATTERN,
    IMAGE_TAG_PATTERN,
    AdapterError,
    AwsEnvironment,
    DeployResult,
    DestroyResult,
    LogEvent,
    OnpremEnvironment,
    StatusResult,
)
from .redact import redact_text
from .ssh import CommandResult, SshConnection, SshRunner

_APP = re.compile(APP_NAME_PATTERN)
_TAG = re.compile(IMAGE_TAG_PATTERN)
HEALTH_PATH = "/healthz"  # 명세의 healthcheck는 현재 /healthz만 허용된다. status와 rollback은 명세가 없어서 이 값을 쓴다.
ServerEnvironment = OnpremEnvironment | AwsEnvironment  # host, ssh_user, ssh_port를 가진 환경


def _err(code: str, message: str, hint: str | None = None, retryable: bool = False) -> AdapterError:
    return AdapterError(code=code, message=message, hint=hint, retryable=retryable)


def _ssh_error(result: CommandResult) -> AdapterError:
    if result.connection_failed or result.timed_out:
        return _err(
            "ssh_unreachable",
            "서버에 SSH로 접속할 수 없습니다.",
            hint="서버 주소, 22번 포트(서비스 서버 IP 허용), 준비 스크립트 실행 여부를 확인해 주세요.",
            retryable=True,
        )
    return _err("ssh_command_failed", "서버에서 명령을 실행하지 못했습니다.")


def _step(log: LogFn, step: int, total: int, name: str, message: str) -> None:
    log(LogEvent(step=step, total=total, name=name, message=message))


def _bad_app() -> AdapterError:
    return _err(
        "invalid_spec",
        "앱 이름 형식이 올바르지 않습니다.",
        hint="앱 이름은 소문자로 시작하는 3~63자의 소문자, 숫자, 하이픈이어야 합니다.",
    )


class ComposeAdapter:
    def __init__(
        self,
        key_path: Path,
        *,
        base_domain: str = "anyship.cloud",
        verify_tls: bool = True,
        connect: Callable[[ServerEnvironment], tuple[SshRunner, ComposeHost]] | None = None,
        healthy=wait_healthy,
    ):
        self._key = key_path
        self._domain = base_domain
        self._verify_tls = verify_tls  # False는 Let's Encrypt staging 인증서를 시험할 때만
        self._connect = connect or self._default_connect  # 시험에서는 가짜 서버로 바꿔 끼운다
        self._healthy = healthy

    def _default_connect(self, env: ServerEnvironment) -> tuple[SshRunner, ComposeHost]:
        ssh = SshRunner(SshConnection(env.host, self._key, env.ssh_user, env.ssh_port))
        return ssh, ComposeHost(ssh)

    def _address(self, env: ServerEnvironment, app: str) -> str:
        raise NotImplementedError

    def status(self, env: ServerEnvironment, app: str) -> StatusResult:
        if not _APP.match(app):
            return StatusResult(ok=False, error=_bad_app())
        ssh, host = self._connect(env)
        reached = ssh.run(["true"])
        if not reached.ok:
            return StatusResult(ok=False, error=_ssh_error(reached))
        if not host.exists(app):
            return StatusResult(ok=True, state="not_deployed")
        url, tag = f"https://{self._address(env, app)}", host.current_image_tag(app)
        if "web" not in host.running_services(app):
            return StatusResult(ok=True, state="stopped", url=url, image_tag=tag)
        healthy, _ = self._healthy(url + HEALTH_PATH, attempts=1, verify_tls=self._verify_tls)
        return StatusResult(
            ok=True, state="running" if healthy else "unhealthy", url=url, image_tag=tag
        )

    def rollback(
        self, env: ServerEnvironment, app: str, image_tag: str, log: LogFn
    ) -> DeployResult:
        """서버에 남아 있는 이전 이미지로 다시 실행한다. DB 스키마는 되돌리지 않는다."""
        if not _APP.match(app):
            return DeployResult(ok=False, error=_bad_app())
        if not _TAG.match(image_tag):
            return DeployResult(
                ok=False,
                error=_err(
                    "invalid_image_tag",
                    "이미지 태그 형식이 올바르지 않습니다.",
                    hint="커밋 SHA(16진수 7~40자)를 사용해 주세요.",
                ),
            )
        ssh, host = self._connect(env)
        total = 4

        def fail(error: AdapterError, result: CommandResult | None = None) -> DeployResult:
            log(LogEvent(level="error", message=error.message))
            details = (
                {"stderr": redact_text(result.stderr[-500:])} if result and result.stderr else {}
            )
            return DeployResult(ok=False, error=error, image_tag=image_tag, details=details)

        _step(log, 1, total, "롤백 준비", f"이미지 {app}:{image_tag}가 서버에 있는지 확인하는 중")
        reached = ssh.run(["true"])
        if not reached.ok:
            return fail(_ssh_error(reached), reached)
        if not host.exists(app):
            return fail(
                _err(
                    "app_not_found",
                    "배포된 앱을 찾을 수 없어 되돌릴 수 없습니다.",
                    hint="먼저 이 환경에 앱을 배포해 주세요.",
                )
            )
        if not host.image_present(f"{app}:{image_tag}"):
            return fail(
                _err(
                    "image_not_found",
                    "서버에 이 버전의 이미지가 남아 있지 않습니다.",
                    hint="이미지를 레지스트리에 보관하지 않아서, 서버에 없는 버전은 다시 배포해야 합니다.",
                )
            )
        _step(log, 2, total, "설정 갱신", "이미지 태그만 바꾸고 나머지 설정은 그대로 둔다")
        updated = host.set_image_tag(app, image_tag)
        if not updated.ok:
            return fail(
                _err("server_write_failed", "서버의 설정 파일을 바꾸지 못했습니다."), updated
            )
        _step(log, 3, total, "앱 시작", "이전 버전으로 다시 시작하는 중")
        started = host.up(app)
        if not started.ok:
            return fail(
                _err(
                    "container_start_failed",
                    "이전 버전의 컨테이너를 시작하지 못했습니다.",
                    retryable=True,
                ),
                started,
            )
        _step(log, 4, total, "헬스체크", "공개 주소로 앱이 응답하는지 확인하는 중")
        url = f"https://{self._address(env, app)}"
        healthy, status = self._healthy(url + HEALTH_PATH, verify_tls=self._verify_tls)
        if not healthy:
            return fail(
                _err(
                    "healthcheck_failed",
                    f"되돌렸지만 헬스체크를 통과하지 못했습니다(마지막 응답: {status}).",
                    retryable=True,
                )
            )
        return DeployResult(ok=True, url=url, image_tag=image_tag)

    def destroy(self, env: ServerEnvironment, app: str, log: LogFn) -> DestroyResult:
        """앱의 컨테이너, 스택의 볼륨, 서버의 앱 디렉터리를 지운다. 이미 없어도 성공이다."""
        if not _APP.match(app):
            return DestroyResult(ok=False, error=_bad_app())
        ssh, host = self._connect(env)
        _step(log, 1, 2, "컨테이너와 데이터 제거", "컨테이너와 볼륨을 지우는 중")
        reached = ssh.run(["true"])
        if not reached.ok:
            return DestroyResult(ok=False, error=_ssh_error(reached))
        if host.exists(app):
            downed = host.down(app)
            if not downed.ok:
                return DestroyResult(
                    ok=False,
                    error=_err(
                        "destroy_failed",
                        "컨테이너를 지우지 못했습니다.",
                        hint="서버에서 docker compose down을 직접 확인해 주세요.",
                    ),
                    details={"stderr": redact_text(downed.stderr[-500:])},
                )
        _step(log, 2, 2, "파일 제거", "서버의 앱 디렉터리를 지우는 중")
        removed = host.remove_dir(app)
        if not removed.ok:
            return DestroyResult(
                ok=False, error=_err("destroy_failed", "앱 디렉터리를 지우지 못했습니다.")
            )
        return DestroyResult(ok=True)

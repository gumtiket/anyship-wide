"""실제 환경 없이 서비스를 개발하고 시험할 수 있는 가짜 어댑터.

실제 어댑터가 지켜야 할 규칙을 그대로 따른다.

  * 받은 입력을 검증하고, 예외를 던지지 않고 AdapterError로 답한다.
  * 진행 상황을 번호가 붙은 단계로 알린다.
  * 비밀 값은 로그와 결과에 절대 내보내지 않고, 이름만 남긴다.

시나리오(만들 때 하나를 고른다):
  success        모든 것이 성공
  check_fails    check()가 환경에 접속하거나 관리하지 못함
  deploy_fails   새 컨테이너가 시작되지 않음(4단계)
  unhealthy      앱은 시작됐지만 헬스체크가 실패함(5단계)

개발과 시험 전용이다. 데모에서 쓰면 mock임을 밝혀야 한다.
"""

import re
import threading
import time
from collections.abc import Callable
from typing import Literal, get_args

from .base import LogFn
from .models import (
    APP_NAME_PATTERN,
    IMAGE_TAG_PATTERN,
    AdapterError,
    AwsEnvironment,
    CheckResult,
    DeployResult,
    DestroyResult,
    Environment,
    LogEvent,
    Secrets,
    Spec,
    StatusResult,
)
from .redact import make_safe_log
from .sets import AWS_ALWAYS_ON, AWS_SERVERLESS, ONPREM, SetName

Scenario = Literal["success", "check_fails", "deploy_fails", "unhealthy"]

DEPLOY_STEPS = ("환경 점검", "이미지 전달", "데이터베이스 준비", "앱 시작", "헬스체크")

_APP = re.compile(APP_NAME_PATTERN)
_TAG = re.compile(IMAGE_TAG_PATTERN)


class MockAdapter:
    def __init__(
        self,
        scenario: Scenario = "success",
        delay: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if scenario not in get_args(Scenario):
            raise ValueError(f"unknown scenario: {scenario!r}")
        self.scenario = scenario
        self._delay = delay
        self._sleep = sleep
        self._lock = threading.Lock()
        # (환경 ID, 앱 이름) -> 배포 정보. 메모리에만 있어서 프로세스가 끝나면 사라진다.
        self._apps: dict[tuple[str, str], dict[str, str]] = {}

    # -- 인터페이스 ------------------------------------------------------------------
    def check(self, env: Environment, log: LogFn) -> CheckResult:
        self._step(log, 1, 2, "접속 확인", "환경에 접속하는 중")
        if self.scenario == "check_fails":
            return CheckResult(ok=False, error=_check_error(env))
        self._step(log, 2, 2, "준비 상태 확인", "필요한 구성 요소를 확인하는 중")
        return CheckResult(ok=True, details=_check_details(env))

    def deploy(
        self,
        env: Environment,
        spec: Spec,
        image_tag: str,
        secrets: Secrets,
        log: LogFn,
        *,
        set_name: SetName,
    ) -> DeployResult:
        error = _validate_deploy(env, spec, image_tag, set_name)
        if error:
            return DeployResult(ok=False, error=error)
        safe_log = make_safe_log(log, secrets)
        return self._run_deploy(env, spec["app"], image_tag, set_name, safe_log, secrets)

    def status(self, env: Environment, app: str) -> StatusResult:
        with self._lock:
            known = self._apps.get((env.env_id, app))
        if not known:
            return StatusResult(ok=True, state="not_deployed")
        return StatusResult(
            ok=True, state="running", url=known["url"], image_tag=known["image_tag"]
        )

    def rollback(self, env: Environment, app: str, image_tag: str, log: LogFn) -> DeployResult:
        if not _TAG.match(image_tag):
            return DeployResult(ok=False, error=_invalid_tag())
        with self._lock:
            known = self._apps.get((env.env_id, app))
        if not known:
            return DeployResult(
                ok=False,
                error=AdapterError(
                    code="app_not_found",
                    message="배포된 앱을 찾을 수 없어 되돌릴 수 없습니다.",
                    hint="먼저 이 환경에 앱을 배포해 주세요.",
                ),
            )
        self._step(log, 1, 2, "이전 버전 준비", f"이미지 {image_tag}를 준비하는 중")
        self._step(log, 2, 2, "앱 시작", "이전 버전으로 다시 시작하는 중")
        with self._lock:
            self._apps[(env.env_id, app)] = {**known, "image_tag": image_tag}
        return DeployResult(ok=True, url=known["url"], image_tag=image_tag)

    def destroy(self, env: Environment, app: str, log: LogFn) -> DestroyResult:
        self._step(log, 1, 1, "앱 제거", "배포한 리소스를 제거하는 중")
        with self._lock:
            self._apps.pop((env.env_id, app), None)  # 없는 앱을 제거하는 것도 성공으로 본다
        return DestroyResult(ok=True)

    # -- 배포 흐름(하위 클래스가 바꿔서 다른 동작을 시험할 수 있다) ------------------------
    def _run_deploy(
        self,
        env: Environment,
        app: str,
        image_tag: str,
        set_name: SetName,
        log: LogFn,
        secrets: Secrets,
    ) -> DeployResult:
        total = len(DEPLOY_STEPS)
        for number, name in enumerate(DEPLOY_STEPS, start=1):
            self._step(log, number, total, name, f"{name} 중", data={"image_tag": image_tag})
            failure = self._failure_at(number)
            if failure:
                log(
                    LogEvent(
                        level="error", step=number, total=total, name=name, message=failure.message
                    )
                )
                return DeployResult(ok=False, error=failure, image_tag=image_tag)
        url = _public_url(env, app, set_name)
        with self._lock:
            self._apps[(env.env_id, app)] = {"image_tag": image_tag, "url": url, "set": set_name}
        # 비밀은 값이 아니라 이름만 남긴다.
        return DeployResult(
            ok=True,
            url=url,
            image_tag=image_tag,
            details={"set": set_name, "secrets_stored": sorted(secrets)},
        )

    def _failure_at(self, step: int) -> AdapterError | None:
        if self.scenario == "deploy_fails" and step == 4:
            return AdapterError(
                code="container_start_failed",
                message="앱 컨테이너가 시작되지 않았습니다.",
                hint="앱의 시작 로그에서 오류를 확인해 주세요. 환경변수와 데이터베이스 연결 설정이 맞는지 점검하세요.",
                retryable=True,
            )
        if self.scenario == "unhealthy" and step == 5:
            return AdapterError(
                code="healthcheck_failed",
                message="앱이 시작됐지만 헬스체크(/healthz)에 응답하지 않습니다.",
                hint="앱이 PORT 환경변수의 포트에서 요청을 받고 /healthz가 200을 반환하는지 확인해 주세요.",
                retryable=True,
            )
        return None

    def _step(
        self, log: LogFn, step: int, total: int, name: str, message: str, data: dict | None = None
    ) -> None:
        if self._delay:
            self._sleep(self._delay)
        log(LogEvent(step=step, total=total, name=name, message=message, data=data or {}))


# -- 보조 함수 ---------------------------------------------------------------------------
def _validate_deploy(
    env: Environment, spec: Spec, image_tag: str, set_name: SetName
) -> AdapterError | None:
    app = spec.get("app") if hasattr(spec, "get") else None
    if not isinstance(app, str) or not _APP.match(app):
        return AdapterError(
            code="invalid_spec",
            message="배포 명세의 앱 이름이 올바르지 않습니다.",
            hint="앱 이름은 소문자로 시작하는 3~63자의 소문자, 숫자, 하이픈이어야 합니다.",
        )
    if not _TAG.match(image_tag):
        return _invalid_tag()
    allowed = (ONPREM,) if env.kind == "onprem" else (AWS_SERVERLESS, AWS_ALWAYS_ON)
    if set_name not in allowed:
        return AdapterError(
            code="set_not_supported",
            message=f"이 환경({env.kind})에서는 '{set_name}' 세트를 사용할 수 없습니다.",
            hint="환경에 맞는 세트를 선택해 주세요.",
        )
    for service in spec.get("backing_services") or []:
        if isinstance(service, dict) and service.get("type") == "object_storage":
            return AdapterError(
                code="unsupported_backing_service",
                message="파일 저장소(object_storage)를 쓰는 앱은 아직 지원하지 않습니다.",
                hint="파일을 로컬 디스크에 저장하지 않도록 앱을 수정해 주세요.",
            )
    return None


def _invalid_tag() -> AdapterError:
    return AdapterError(
        code="invalid_image_tag",
        message="이미지 태그 형식이 올바르지 않습니다.",
        hint="커밋 SHA(16진수 7~40자)를 사용해 주세요.",
    )


def _check_error(env: Environment) -> AdapterError:
    if env.kind == "onprem":
        return AdapterError(
            code="ssh_unreachable",
            message="서버에 SSH로 접속할 수 없습니다.",
            hint="서버 주소와 22번 포트 허용(서비스 서버 IP) 여부, 준비 스크립트 실행 여부를 확인해 주세요.",
            retryable=True,
        )
    return AdapterError(
        code="assume_role_denied",
        message="사용자 AWS 계정의 역할을 사용할 수 없습니다.",
        hint="역할 ARN과 External ID가 맞는지, 방금 만든 역할이라면 잠시 후 다시 시도해 주세요.",
        retryable=True,
    )


def _check_details(env: Environment) -> dict:
    if isinstance(env, AwsEnvironment):
        return {"account_id": env.role_arn.split(":")[4], "region": env.region}
    return {"public_ip": "203.0.113.10"}  # 문서용 예시 주소. 이 어댑터는 mock이다.


def _public_url(env: Environment, app: str, set_name: SetName) -> str:
    if set_name == ONPREM:
        return f"https://{app}.{env.env_id}.onprem.anyship.cloud"
    if set_name == AWS_ALWAYS_ON:
        return f"https://{app}.{env.env_id}.aws.anyship.cloud"
    return f"https://{app}.lambda-url.{env.region}.on.aws/"  # type: ignore[union-attr]

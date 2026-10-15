"""서비스가 호출하는 인터페이스.

모든 환경 어댑터(와 mock)가 이 인터페이스를 구현하므로, 서비스는 환경이 어떻게
동작하는지 몰라도 된다.

모든 메서드는 작업이 끝날 때까지 기다린다. 서비스가 백그라운드 작업자에서
실행하고, 로그 이벤트를 사용자에게 흘려보낸다. 실패는 예외가 아니라 `ok=False`
결과로 돌려준다(예상하지 못한 버그는 그대로 예외가 된다).
"""

from collections.abc import Callable
from typing import Protocol

from .models import (
    CheckResult,
    DeployResult,
    DestroyResult,
    Environment,
    LogEvent,
    Secrets,
    Spec,
    StatusResult,
)
from .sets import SetName

LogFn = Callable[[LogEvent], None]


class Adapter(Protocol):
    def check(self, env: Environment, log: LogFn) -> CheckResult:
        """이 환경에 접속하고 관리할 수 있는지 확인한다(연결, 권한, 사전 준비)."""
        ...

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
        """선택한 세트로 `spec`을 `image_tag` 버전으로 배포하고 접속 주소를 돌려준다.

        `secrets`는 메모리에만 있다. 어댑터가 환경의 비밀 저장소(AWS Secrets
        Manager 또는 서버의 .env)에 쓰고, 로그에는 절대 남기지 않는다.
        """
        ...

    def status(self, env: Environment, app: str) -> StatusResult:
        """앱이 지금 실행 중이고 정상인지 확인한다."""
        ...

    def rollback(self, env: Environment, app: str, image_tag: str, log: LogFn) -> DeployResult:
        """이전 이미지 태그로 다시 실행한다. 데이터베이스 스키마는 되돌리지 않는다."""
        ...

    def destroy(self, env: Environment, app: str, log: LogFn) -> DestroyResult:
        """이 앱을 위해 배포가 만든 것을 제거한다."""
        ...

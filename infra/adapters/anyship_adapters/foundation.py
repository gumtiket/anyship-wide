"""사용자 계정의 공용 기반(`infra/user-account`)이 있으면 읽어서 쓰고, 없으면 만든다(`ensure_foundation`).

기반은 사용자 계정마다 한 번만 만들고 모든 앱이 공유한다. 첫 배포에는 만드는 데 약 20분이 걸리고, 이후에는 state의 출력만 읽어
약 10초면 끝난다. 이 함수는 TerraformRunner의 `read_foundation`과 `apply`를 순서대로 엮을 뿐이다.

만들어도 되는 경우는 **state가 비어 있을 때뿐**이다. state_bucket이 없거나, 역할을 맡지 못했거나, 다른 작업이 잠금을 잡고 있거나,
출력이 이상한 경우에는 20분짜리 apply를 시작하지 않고 그대로 오류를 올린다. apply가 도중에 끊긴 경우(출력이 아직 기록되기 전)는
state가 비어 있는 것과 같아서, 다시 부르면 만들어진 것은 이어서 진행된다.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .base import LogFn
from .models import AwsEnvironment, LogEvent
from .terraform_runner import TerraformError, TerraformRunner

# 서비스가 바꿀 수 있는 선택 변수. 값의 범위는 Terraform의 validation이 강제한다(infra/user-account/variables.tf).
OPTIONAL_VARIABLES = frozenset(
    {
        "vpc_cidr",
        "host_instance_type",
        "host_volume_size",
        "db_instance_class",
        "db_allocated_storage",
    }
)


@dataclass(frozen=True)
class FoundationSettings:
    """환경이 아니라 **서비스**가 아는 값. 기반을 만들 때만 쓴다(비밀이 아니다. 개인 키는 넣지 않는다)."""

    service_server_ip: str  # 호스트의 SSH(22)를 이 주소에만 연다
    ssh_public_key: str  # 서비스 서버 배포 키의 공개 키(ssh-ed25519 ...)
    acme_email: str  # Traefik의 Let's Encrypt 연락 주소
    overrides: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        unknown = sorted(set(self.overrides) - OPTIONAL_VARIABLES)
        if unknown:  # region, account_id, env_id 같은 필수 값을 덮어쓰지 못하게 한다
            raise ValueError("unknown foundation overrides: " + ", ".join(unknown))

    def variables(self, env: AwsEnvironment) -> dict[str, Any]:
        return {
            "region": env.region,
            "account_id": env.role_arn.split(":")[4],
            "env_id": env.env_id,
            "service_server_ip": self.service_server_ip,
            "ssh_public_key": self.ssh_public_key,
            "acme_email": self.acme_email,
            **self.overrides,
        }


@dataclass(frozen=True)
class Foundation:
    env: AwsEnvironment  # 기반 필드(host, db_address, db_port, db_secret_arn)가 채워진 환경
    created: bool  # 이번 호출이 새로 만들었으면 True

    def fields(self) -> dict[str, Any]:
        """서비스가 환경 레코드에 저장할 값. 비밀이 아니다(비밀번호는 Secrets Manager에만 있다)."""
        return {
            "host": self.env.host,
            "db_address": self.env.db_address,
            "db_port": self.env.db_port,
            "db_secret_arn": self.env.db_secret_arn,
        }


def ensure_foundation(
    runner: TerraformRunner, env: AwsEnvironment, settings: FoundationSettings, log: LogFn
) -> Foundation:
    try:
        return Foundation(runner.read_foundation(env, log), created=False)
    except TerraformError as exc:
        # state_bucket이 없어서 나온 foundation_missing은 "기반이 없다"가 아니라 "어디에 있는지 모른다"이므로 만들지 않는다.
        if exc.error.code != "foundation_missing" or env.state_bucket is None:
            raise
    log(LogEvent(message="공용 기반이 아직 없어 새로 만듭니다(첫 배포만, 약 20분 걸립니다)."))
    runner.apply(env, settings.variables(env), log)
    return Foundation(runner.read_foundation(env, log), created=True)

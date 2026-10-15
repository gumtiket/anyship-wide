import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import APP_NAME_PATTERN, AdapterError


class SpecError(Exception):
    """명세나 입력을 거부할 때 던진다. 어댑터가 잡아서 결과의 오류로 바꾼다."""

    def __init__(self, error: AdapterError):
        super().__init__(error.message)
        self.error = error


def reject(code: str, message: str, hint: str | None = None) -> SpecError:
    return SpecError(AdapterError(code=code, message=message, hint=hint))


ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
MAX_VALUE_LENGTH = 1024
# 값은 .env 파일에 작은따옴표로 감싸 쓰므로, 줄바꿈과 작은따옴표는 표현할 수 없다.
FORBIDDEN_VALUE_CHARS = ("\n", "\r", "\0", "'")

# 앱이 마음대로 정하면 서버나 컨테이너 동작을 바꾸거나 어댑터가 주입하는 값과 충돌한다.
RESERVED_NAMES = frozenset(
    {"PATH", "HOME", "USER", "SHELL", "PWD", "HOSTNAME", "DATABASE_URL", "STORAGE_URL"}
)
RESERVED_PREFIXES = ("LD_", "DOCKER_", "AWS_", "COMPOSE_", "TRAEFIK_", "POSTGRES")


class _Loose(BaseModel):
    # 어댑터가 쓰지 않는 필드(source, build, profile 등)는 무시한다.
    model_config = ConfigDict(extra="ignore", frozen=True, coerce_numbers_to_str=True)


class EnvVar(_Loose):
    name: str
    secret: bool = False
    generate: bool = False
    value: str | None = None


class BackingService(_Loose):
    type: str
    bind_as: str | None = None


class Release(_Loose):
    migrate: str


class Web(_Loose):
    instances: int = 1


class Processes(_Loose):
    web: Web = Field(default_factory=Web)


class AppSpec(_Loose):
    app: str = Field(pattern=APP_NAME_PATTERN)
    port: int = Field(default=8080, ge=1024, le=65535)
    healthcheck: str = Field(default="/healthz", pattern=r"^/[A-Za-z0-9/_.-]{0,200}$")
    env: list[EnvVar] = Field(default_factory=list)
    backing_services: list[BackingService] = Field(default_factory=list)
    release: Release | None = None
    processes: Processes = Field(default_factory=Processes)


@dataclass(frozen=True)
class ParsedSpec:
    app: str
    port: int
    healthcheck: str
    env: tuple[EnvVar, ...]  # 건너뛰기로 한 항목(PORT 등)은 빠져 있다
    postgres: bool
    migrate: str | None
    warnings: tuple[str, ...]


def check_value(name: str, value: str) -> None:
    """환경변수 값이 .env 파일에 안전하게 쓸 수 있는 값인지 확인한다."""
    if len(value) > MAX_VALUE_LENGTH or any(ch in value for ch in FORBIDDEN_VALUE_CHARS):
        raise reject(
            "invalid_env_value",
            f"환경변수 {name}의 값을 사용할 수 없습니다.",
            hint=f"값은 {MAX_VALUE_LENGTH}자 이하여야 하고 줄바꿈과 작은따옴표(')를 포함할 수 없습니다.",
        )


def _check_env(variables: list[EnvVar], port: int) -> tuple[tuple[EnvVar, ...], list[str]]:
    kept: list[EnvVar] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for var in variables:
        if not ENV_NAME.match(var.name):
            raise reject(
                "invalid_env_name",
                "환경변수 이름 형식이 올바르지 않습니다.",
                hint="대문자, 숫자, 밑줄만 쓰고 숫자로 시작하지 않아야 합니다(최대 64자).",
            )
        if var.name in seen:
            raise reject("invalid_env_name", f"환경변수 {var.name}이(가) 중복되었습니다.")
        seen.add(var.name)

        if var.name == "PORT":
            if var.secret or var.generate or var.value != str(port):
                raise reject(
                    "invalid_env_name",
                    "환경변수 PORT는 port 필드와 같은 값이어야 합니다.",
                    hint="PORT는 어댑터가 port 필드에서 주입합니다. env에서 지워 주세요.",
                )
            warnings.append("env의 PORT는 port 필드에서 주입하므로 무시했습니다.")
            continue
        if var.name in RESERVED_NAMES or var.name.startswith(RESERVED_PREFIXES):
            raise reject(
                "invalid_env_name",
                f"환경변수 {var.name}은(는) 사용할 수 없는 이름입니다.",
                hint="시스템이나 어댑터가 사용하는 이름입니다. 다른 이름을 써 주세요.",
            )

        if var.generate and not var.secret:
            raise reject(
                "invalid_secret_config",
                f"환경변수 {var.name}: generate는 secret일 때만 쓸 수 있습니다.",
            )
        if var.secret and var.value is not None:
            raise reject(
                "invalid_secret_config",
                f"환경변수 {var.name}: 비밀 값은 명세에 적을 수 없습니다.",
                hint="비밀은 생성(generate)하거나 배포 때 입력으로 전달해야 합니다.",
            )
        if not var.secret:
            if var.value is None:
                raise reject(
                    "invalid_secret_config", f"환경변수 {var.name}: 일반 설정은 value가 필요합니다."
                )
            check_value(var.name, var.value)
        kept.append(var)
    return tuple(kept), warnings


def parse_spec(spec: Mapping[str, Any]) -> ParsedSpec:
    if not isinstance(spec, Mapping):
        raise reject("invalid_spec", "배포 명세의 형식이 올바르지 않습니다.")
    try:
        model = AppSpec.model_validate(spec)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(part) for part in err["loc"]) or "(명세 전체)" for err in exc.errors()}
        )
        raise reject(
            "invalid_spec",
            "배포 명세의 다음 항목이 올바르지 않습니다: " + ", ".join(fields),
            hint="앱 이름, 포트, 헬스체크 경로, 환경변수, 외부 자원 항목을 확인해 주세요.",
        ) from None

    if model.processes.web.instances != 1:
        raise reject("invalid_spec", "웹 프로세스는 인스턴스 1개만 지원합니다.")

    env, warnings = _check_env(model.env, model.port)

    postgres = False
    for service in model.backing_services:
        if service.type == "postgres":
            if postgres:
                raise reject("invalid_spec", "postgres 외부 자원이 두 번 선언되었습니다.")
            if service.bind_as not in (None, "DATABASE_URL"):
                raise reject("invalid_spec", "postgres는 DATABASE_URL로만 연결할 수 있습니다.")
            postgres = True
        elif service.type == "object_storage":
            warnings.append(
                "파일 저장소(object_storage)는 아직 지원하지 않아 건너뛰었습니다. "
                "앱이 파일을 저장한다면 재배포 때 사라질 수 있습니다."
            )
        else:
            raise reject(
                "unsupported_backing_service",
                "지원하지 않는 외부 자원이 선언되었습니다.",
                hint="현재는 postgres만 지원합니다.",
            )

    migrate = model.release.migrate if model.release else None
    if migrate is not None and (
        not migrate.strip() or len(migrate) > 500 or "\n" in migrate or "\0" in migrate
    ):
        raise reject(
            "invalid_spec",
            "release.migrate 명령이 올바르지 않습니다.",
            hint="한 줄의 명령(500자 이하)이어야 합니다.",
        )

    return ParsedSpec(
        app=model.app,
        port=model.port,
        healthcheck=model.healthcheck,
        env=env,
        postgres=postgres,
        migrate=migrate,
        warnings=tuple(warnings),
    )

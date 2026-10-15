"""B's defensive env policy. 확인 필요(C 확정 전): share the final policy with C."""

import re

ENV_NAME = r"^[A-Z][A-Z0-9_]{1,63}$"
APP_NAME = r"^[a-z][a-z0-9-]{1,29}[a-z0-9]$"
MAX_ENV_BYTES = 4096
MAX_ENV_VALUE_CHARS = 1024
# PORT는 spec.port에서 온다. 플랫폼·프로세스 제어 값은 앱 설정에서 오면 안 된다.
DENIED_NAMES = frozenset(
    {
        "PORT",
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "PWD",
        "HOSTNAME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "NODE_OPTIONS",
        "RUBYOPT",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "CDPATH",
        "_HANDLER",
        "_X_AMZN_TRACE_ID",
        "LAMBDA_TASK_ROOT",
        "LAMBDA_RUNTIME_DIR",
        "LAMBDA_RUNTIME_API",
    }
)
DENIED_PREFIXES = ("AWS_", "DOCKER_", "LAMBDA_", "COMPOSE_", "TRAEFIK_", "LD_", "POSTGRES")
EXTERNAL_URLS = frozenset({"DATABASE_URL", "REDIS_URL", "STORAGE_URL"})


def allowed_name(name: str) -> bool:
    return (
        bool(re.fullmatch(ENV_NAME, name))
        and name not in DENIED_NAMES
        and name not in EXTERNAL_URLS
        and not name.startswith(DENIED_PREFIXES)
    )


def allowed_value(value: str) -> bool:
    return len(value) <= MAX_ENV_VALUE_CHARS and not any(c in value for c in "\r\n\x00'")


def known_env_bytes(values: dict[str, str]) -> int:
    """알려진 UTF-8 키·값 바이트만 센다.

    배포 쪽은 모든 비밀과 URL을 주입한 뒤 다시 확인해야 한다.
    """
    return sum(len(key.encode()) + len(value.encode()) for key, value in values.items())


def app_name_from_directory(name: str) -> str:
    if re.fullmatch(APP_NAME, name):
        return name
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = "app-" + slug
    slug = slug[:31].rstrip("-")
    return slug if len(slug) >= 3 else "app-" + slug

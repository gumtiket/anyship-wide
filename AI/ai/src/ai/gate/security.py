import re
from collections.abc import Collection, Mapping

SECURITY_FLAGS = (
    "--read-only",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,size=512m,mode=1777",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--memory",
    "512m",
    "--cpus",
    "1",
    "--pids-limit",
    "128",
)
APP_ENV = frozenset({"PORT", "DATABASE_URL", "LOG_LEVEL", "SECRET_KEY"})
DB_ENV = frozenset({"POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD", "PGDATA", "PGHOST"})
PG_SOCKET_TMPFS = "/var/run/postgresql:rw,noexec,nosuid,size=16m,uid=70,gid=70,mode=3775"
OWNER_LABEL = "bronze.ai-gate=true"
NAME = re.compile(r"^bronze-gate-[a-f0-9]{12}(?:-[a-z0-9-]+)?$")
IMAGE = re.compile(
    r"^(?:bronze-ai-gate:gate-[a-f0-9]{24}|postgres:16-alpine|curlimages/curl:8\.12\.1)$"
)


def validate_run_args(
    args: list[str],
    *,
    internal_networks: Collection[str],
    allowed_env: Collection[str],
) -> None:
    """엄격한 허용 목록 파서. 중복 옵션과 알 수 없는 Docker 옵션은 모두 거부한다."""
    if not args or args[0] != "run":
        raise ValueError("gate_run_required")
    values = {}
    env = {}
    tmpfs = []
    switches = set()
    i = 1
    value_flags = {
        "--name",
        "--label",
        "--tmpfs",
        "--cap-drop",
        "--security-opt",
        "--memory",
        "--cpus",
        "--pids-limit",
        "--user",
        "--network",
    }
    while i < len(args) and args[i].startswith("-"):
        flag = args[i]
        if flag in {"--read-only", "--detach", "--pull=never"}:
            if flag in switches:
                raise ValueError("gate_duplicate_flag")
            switches.add(flag)
            i += 1
        elif flag in value_flags | {"--env"}:
            if i + 1 >= len(args):
                raise ValueError("gate_missing_flag_value")
            value = args[i + 1]
            if flag == "--tmpfs":
                if value in tmpfs:
                    raise ValueError("gate_duplicate_flag")
                tmpfs.append(value)
            elif flag == "--env":
                name, separator, content = value.partition("=")
                if not separator or name not in allowed_env or name in env or "\0" in content:
                    raise ValueError("gate_environment_not_allowed")
                env[name] = content
            else:
                if flag in values:
                    raise ValueError("gate_duplicate_flag")
                values[flag] = value
            i += 2
        else:
            raise ValueError("gate_forbidden_flag")
    expected = {
        "--cap-drop": "ALL",
        "--security-opt": "no-new-privileges",
        "--memory": "512m",
        "--cpus": "1",
        "--pids-limit": "128",
        "--label": OWNER_LABEL,
    }
    if not {"--read-only", "--detach", "--pull=never"} <= switches or any(
        values.get(k) != v for k, v in expected.items()
    ):
        raise ValueError("gate_security_flags_missing")
    if values.get("--user") not in {"10001:10001", "70:70"}:
        raise ValueError("gate_nonroot_required")
    if values.get("--network") not in internal_networks:
        raise ValueError("gate_internal_network_required")
    if not NAME.fullmatch(values.get("--name", "")):
        raise ValueError("gate_owned_name_required")
    if i >= len(args) or not IMAGE.fullmatch(args[i]):
        raise ValueError("gate_image_not_allowed")
    image = args[i]
    expected_tmpfs = {SECURITY_FLAGS[2]}
    if image == "postgres:16-alpine":
        expected_tmpfs.add(PG_SOCKET_TMPFS)
    if set(tmpfs) != expected_tmpfs:
        raise ValueError("gate_tmpfs_not_allowed")
    if image == "postgres:16-alpine":
        if values["--user"] != "70:70" or not set(env) <= DB_ENV:
            raise ValueError("gate_db_environment_required")
    elif not set(env) <= APP_ENV:
        raise ValueError("gate_app_environment_required")
    if "docker.sock" in " ".join(args[:i]):
        raise ValueError("gate_socket_forbidden")


def run_args(
    *,
    image: str,
    name: str,
    network: str,
    env: Mapping[str, str],
    command: list[str],
) -> list[str]:
    args = [
        "run",
        "--pull=never",
        "--detach",
        "--name",
        name,
        "--label",
        OWNER_LABEL,
        *SECURITY_FLAGS,
        "--user",
        "70:70" if image == "postgres:16-alpine" else "10001:10001",
        "--network",
        network,
    ]
    for key, value in sorted(env.items()):
        args += ["--env", f"{key}={value}"]
    if image == "postgres:16-alpine":
        args += ["--tmpfs", PG_SOCKET_TMPFS]
    return [*args, image, *command]

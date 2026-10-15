import json
import re
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai.llm.base import LLMClient

# TODO: 최신 확인. AWS 공식 README에서 2026-10-08 확인; 실행마다 조회하지 않는다.
LWA_TAG = "1.1.0"
DOCKERIGNORE = """.git
.venv
venv
__pycache__
*.py[cod]
*.db
*.sqlite*
.[eE][nN][vV]*
.aws
.ssh
*.pem
*.key
*.log
out
output
.pytest_cache
.ruff_cache
Dockerfile*
VIOLATIONS.json
*.expected.json
"""


def harden_dockerignore(existing: str = "") -> str:
    if existing.endswith(DOCKERIGNORE):
        return existing
    return (existing.rstrip() + "\n" if existing else "") + DOCKERIGNORE


class DockerInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    python_version: str = Field(default="3.12", pattern=r"^3\.12$")
    system_packages: list[Literal["libpq5"]] = Field(default_factory=list, max_length=1)
    # 탐지한 진입점이 기준이다. 모델이 다른 명령을 끼워 넣을 수 없다.
    entrypoint: str | None = Field(
        default=None, pattern=r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$"
    )


def render_dockerfile(inputs: DockerInputs, entrypoint: str) -> str:
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", entrypoint):
        raise ValueError("invalid_entrypoint")
    if inputs.entrypoint is not None and inputs.entrypoint != entrypoint:
        raise ValueError("entrypoint_mismatch")
    if any(package != "libpq5" for package in inputs.system_packages):
        raise ValueError("system_package_not_allowed")
    packages = (
        "RUN apt-get update && apt-get install -y --no-install-recommends libpq5 "
        "&& rm -rf /var/lib/apt/lists/*\n"
        if inputs.system_packages
        else ""
    )
    return (
        f"FROM python:{inputs.python_version}-slim\n"
        f"COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:{LWA_TAG} "
        "/lambda-adapter /opt/extensions/lambda-adapter\n"
        "ENV PYTHONDONTWRITEBYTECODE=1\nENV PYTHONUNBUFFERED=1\n"
        "ENV XDG_CACHE_HOME=/tmp/.cache\nENV HOME=/tmp\n"
        "ENV AWS_LWA_READINESS_CHECK_PATH=/healthz\nENV PORT=8080\n"
        "WORKDIR /app\n" + packages + "COPY requirements*.txt ./\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app\n"
        "COPY --chown=10001:10001 . .\nUSER 10001:10001\nEXPOSE 8080\n"
        f'CMD ["sh", "-c", "exec uvicorn {entrypoint} --host 0.0.0.0 --port ${{PORT}}"]\n'
    )


def lint_dockerfile(text: str) -> list[str]:
    lines = [
        line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]
    errors = []
    if not lines or not re.fullmatch(r"FROM python:3\.12-slim", lines[0]):
        errors.append("fixed_slim_base")
    adapter = (
        f"COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:{LWA_TAG} "
        "/lambda-adapter /opt/extensions/lambda-adapter"
    )
    for code, required in {
        "lambda_adapter": adapter,
        "readiness_path": "ENV AWS_LWA_READINESS_CHECK_PATH=/healthz",
        "port_env": "ENV PORT=8080",
        "no_bytecode": "ENV PYTHONDONTWRITEBYTECODE=1",
        "cache_tmp": "ENV XDG_CACHE_HOME=/tmp/.cache",
        "home_tmp": "ENV HOME=/tmp",
        "nonroot": "USER 10001:10001",
    }.items():
        if required not in lines:
            errors.append(code)
    cache_steps = (
        "COPY requirements*.txt ./",
        "RUN pip install --no-cache-dir -r requirements.txt",
        "COPY --chown=10001:10001 . .",
    )
    if any(step not in lines for step in cache_steps) or [
        lines.index(step) for step in cache_steps if step in lines
    ] != sorted(lines.index(step) for step in cache_steps if step in lines):
        errors.append("layer_order")
    if any(
        re.search(r"^(ARG|HEALTHCHECK)\b|\b(date|BUILD_SHA|BUILD_TIME|COMMIT_SHA)\b", line, re.I)
        for line in lines
    ):
        errors.append("build_metadata_or_healthcheck")
    if any(":latest" in line or re.match(r"USER\s+(root|0)(?:\s|:|$)", line) for line in lines):
        errors.append("latest_or_root")
    commands = [line[4:] for line in lines if line.startswith("CMD ")]
    try:
        command = json.loads(commands[0]) if len(commands) == 1 else []
        valid = (
            isinstance(command, list)
            and len(command) == 3
            and command[:2] == ["sh", "-c"]
            and re.fullmatch(
                r"exec uvicorn [A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w* "
                r"--host 0\.0\.0\.0 --port \$\{PORT\}",
                command[2],
            )
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        errors.append("port_command")
    # 템플릿과 정확히 비교하므로 USER/FROM/RUN/ENV 명령이 추가되어도 거부된다.
    if valid:
        entrypoint = command[2].split()[2]
        options = DockerInputs(
            system_packages=["libpq5"] if any("apt-get" in line for line in lines) else []
        )
        if text != render_dockerfile(options, entrypoint):
            errors.append("template_mismatch")
    return errors


def generate_dockerfile(entrypoint: str, llm: LLMClient | None) -> tuple[str, bool]:
    fallback = False
    inputs = DockerInputs()
    if llm is not None:

        class DetectedInputs(DockerInputs):
            @model_validator(mode="after")
            def detected_command(self):
                if self.entrypoint not in {None, entrypoint}:
                    raise ValueError("entrypoint_mismatch")
                return self

        try:
            response = llm.complete(
                files("ai").joinpath("prompts/dockerfile.txt").read_text(),
                json.dumps({"entrypoint": entrypoint, "python_version": "3.12"}),
                tier="strong",
                schema=DetectedInputs,
                stage="dockerfile",
            )
            if not isinstance(response.parsed, DockerInputs):
                raise ValueError("docker_inputs_invalid")
            inputs = response.parsed
        except (ValueError, RuntimeError):
            fallback = True
    try:
        text = render_dockerfile(inputs, entrypoint)
        if lint_dockerfile(text):
            raise ValueError("docker_lint_failed")
    except ValueError:
        text, fallback = render_dockerfile(DockerInputs(), entrypoint), True
    return text, fallback

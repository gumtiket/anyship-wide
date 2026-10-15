import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from tempfile import gettempdir
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from ai.gate.security import APP_ENV, DB_ENV, NAME, OWNER_LABEL, run_args, validate_run_args

POSTGRES_IMAGE = "postgres:16-alpine"
CURL_IMAGE = "curlimages/curl:8.12.1"


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: int
    output: str = ""


class RunnerError(RuntimeError):
    pass


class ContainerRunner(Protocol):
    real: bool

    def preflight(self) -> None: ...
    def build(self, root: Path, tag: str) -> CommandResult: ...
    def network_create(self, name: str) -> None: ...
    def network_remove(self, name: str) -> None: ...
    def run(
        self, *, image: str, name: str, network: str, env: Mapping[str, str], command: list[str]
    ) -> None: ...
    def wait(self, name: str, timeout: int = 30) -> CommandResult: ...
    def logs(self, name: str) -> str: ...
    def remove(self, name: str) -> None: ...
    def image_remove(self, tag: str) -> None: ...


class DockerCliRunner:
    real = True

    def __init__(self) -> None:
        self.networks: set[str] = set()
        self.containers: set[str] = set()
        self.images: set[str] = set()
        self.image_locks: dict[str, int] = {}

    def _call(self, args: list[str], timeout: int = 30) -> CommandResult:
        try:
            completed = subprocess.run(
                ["docker", *args], capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError:
            raise RunnerError("docker_cli_not_found") from None
        except subprocess.TimeoutExpired:
            raise RunnerError("docker_command_timeout") from None
        return CommandResult(
            code=completed.returncode, output=(completed.stdout + completed.stderr)[-40000:]
        )

    def preflight(self) -> None:
        if os.name != "posix":
            raise RunnerError("docker_gate_requires_posix")
        if self._call(["info", "--format", "{{.ServerVersion}}"], timeout=10).code:
            raise RunnerError("docker_daemon_unavailable")
        for image in (POSTGRES_IMAGE, CURL_IMAGE):
            if self._call(["image", "inspect", image, "--format", "{{.Id}}"], timeout=10).code:
                raise RunnerError(f"prepull_required:{image}")

    def build(self, root: Path, tag: str) -> CommandResult:
        if os.name != "posix":
            raise RunnerError("docker_gate_requires_posix")
        # POSIX 잠금은 실제 Docker 실행기에만 필요하다. 오프라인 분석과 FakeRunner는
        # Windows 서비스 호스트에서도 import할 수 있어야 한다.
        import fcntl

        if not re.fullmatch(r"bronze-ai-gate:gate-[a-f0-9]{24}", tag):
            raise ValueError("gate_image_tag_invalid")
        directory = Path(gettempdir()) / "bronze-ai-gate-locks"
        directory.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(
            directory / tag.replace(":", "-"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise RunnerError("gate_image_in_use") from None
        self.image_locks[tag] = descriptor
        try:
            if self._call(["image", "inspect", tag, "--format", "{{.Id}}"]).code == 0:
                raise RunnerError("gate_image_already_exists")
        except BaseException:
            os.close(self.image_locks.pop(tag))
            raise
        # 게이트 이미지는 일회용이다. 빌드 전에 기록해 두면 시간 초과된 빌드도 정리된다.
        self.images.add(tag)
        return self._call(
            ["build", "--label", OWNER_LABEL, "-t", tag, "-f", str(root / "Dockerfile"), str(root)],
            timeout=600,
        )

    def network_create(self, name: str) -> None:
        if not NAME.fullmatch(name):
            raise ValueError("gate_network_name_invalid")
        result = self._call(["network", "create", "--internal", "--label", OWNER_LABEL, name])
        if result.code:
            raise RunnerError("gate_network_create_failed")
        self.networks.add(name)

    def network_remove(self, name: str) -> None:
        if name in self.networks:
            if self._call(["network", "rm", name]).code:
                raise RunnerError("gate_network_cleanup_failed")
            self.networks.remove(name)

    def run(
        self, *, image: str, name: str, network: str, env: Mapping[str, str], command: list[str]
    ) -> None:
        args = run_args(image=image, name=name, network=network, env=env, command=command)
        validate_run_args(
            args,
            internal_networks=self.networks,
            allowed_env=DB_ENV if image == POSTGRES_IMAGE else APP_ENV,
        )
        self.containers.add(name)
        if self._call(args).code:
            raise RunnerError("gate_container_start_failed")

    def wait(self, name: str, timeout: int = 30) -> CommandResult:
        if name not in self.containers:
            raise ValueError("gate_unowned_container")
        result = self._call(["wait", name], timeout=timeout)
        if result.code:
            raise RunnerError("gate_wait_failed")
        try:
            code = int(result.output.strip())
        except ValueError:
            raise RunnerError("gate_wait_output_invalid") from None
        return CommandResult(code=code, output=self.logs(name))

    def logs(self, name: str) -> str:
        if name not in self.containers:
            raise ValueError("gate_unowned_container")
        return self._call(["logs", "--tail", "200", name]).output

    def remove(self, name: str) -> None:
        if name in self.containers:
            # -v는 이 일회용 컨테이너에 붙은 익명 볼륨만 지운다.
            if self._call(["rm", "--force", "--volumes", name]).code:
                raise RunnerError("gate_container_cleanup_failed")
            self.containers.remove(name)

    def image_remove(self, tag: str) -> None:
        try:
            if tag in self.images:
                result = self._call(["image", "rm", tag])
                if result.code and "No such image" not in result.output:
                    raise RunnerError("gate_image_cleanup_failed")
                self.images.remove(tag)
        finally:
            if tag in self.image_locks:
                os.close(self.image_locks.pop(tag))


class FakeRunner:
    real = False

    def __init__(self, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.events: list[tuple[str, str]] = []
        self.commands: list[list[str]] = []
        self.networks: set[str] = set()
        self.containers: dict[str, tuple[str, list[str]]] = {}
        self.envs: dict[str, dict[str, str]] = {}
        self.images: set[str] = set()

    def preflight(self) -> None:
        if self.fail_at == "preflight":
            raise RunnerError("docker_daemon_unavailable")

    def build(self, root: Path, tag: str) -> CommandResult:
        self.events.append(("build", tag))
        self.images.add(tag)
        return CommandResult(code=int(self.fail_at == "build"), output="fake build")

    def network_create(self, name: str) -> None:
        self.events.append(("network_create", name))
        if self.fail_at == "network":
            raise RunnerError("gate_network_create_failed")
        self.networks.add(name)

    def network_remove(self, name: str) -> None:
        self.events.append(("network_remove", name))
        self.networks.discard(name)

    def run(
        self, *, image: str, name: str, network: str, env: Mapping[str, str], command: list[str]
    ) -> None:
        args = run_args(image=image, name=name, network=network, env=env, command=command)
        validate_run_args(
            args,
            internal_networks=self.networks,
            allowed_env=DB_ENV if image == POSTGRES_IMAGE else APP_ENV,
        )
        self.commands.append(args)
        self.events.append(("run", name))
        self.containers[name] = (image, command)
        self.envs[name] = dict(env)

    def wait(self, name: str, timeout: int = 30) -> CommandResult:
        image, command = self.containers[name]
        failure = (
            self.fail_at == "postgres"
            and command[:1] == ["pg_isready"]
            or self.fail_at == "migrate"
            and image.startswith("bronze-ai-gate:gate-")
            or self.fail_at == "health"
            and image == CURL_IMAGE
        )
        return CommandResult(
            code=int(failure), output="fake failure" if failure else self.logs(name)
        )

    def logs(self, name: str) -> str:
        image, command = self.containers[name]
        if "-original" in name:
            return "PermissionError: [Errno 30] Read-only file system: 'app.log'"
        if image == CURL_IMAGE:
            if "-verify-deleted" in name:
                return "[]"
            if "-verify-updated" in name:
                return '[{"id":1,"title":"gate-validation-updated","done":true,"overdue":false}]'
            if "POST" in command:
                return '{"id":1,"title":"gate-validation","done":false,"overdue":false}'
            if "PUT" in command:
                return '{"id":1,"title":"gate-validation-updated","done":true,"overdue":false}'
            if "DELETE" in command:
                return ""
            if any("/todos" in word for word in command):
                return '[{"id":1,"title":"gate-validation","done":false,"overdue":false}]'
            return '{"status":"ok"}'
        return "fake stdout"

    def remove(self, name: str) -> None:
        self.events.append(("remove", name))
        self.containers.pop(name, None)

    def image_remove(self, tag: str) -> None:
        self.events.append(("image_remove", tag))
        self.images.discard(tag)

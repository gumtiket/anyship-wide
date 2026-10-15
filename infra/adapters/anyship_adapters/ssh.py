"""서버에 SSH로 명령을 실행하고 파일을 쓰는 실행기(Compose를 쓰는 세트가 함께 쓰는 부품).

원칙:
  * 명령은 문자열 한 줄이 아니라 **인자 목록**으로 받는다. 원격 셸에 넘길 때 인자마다
    따옴표를 씌우므로, 값에 공백이나 `;`가 있어도 한 덩어리로만 전달된다(명령 주입 방지).
  * 접속 정보는 만들 때 형식을 검증한다. 주소가 `-`로 시작해 ssh 옵션으로 읽히는 일을 막는다.
  * 비밀번호를 묻지 않는다(키 로그인 전용). 물어야 하는 상황이면 멈추지 않고 실패한다.
  * 출력에는 비밀이 섞일 수 있다. 로그로 내보내기 전에 호출하는 쪽이 redact를 적용한다.
"""

import os
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")
_MODE = re.compile(r"^0[0-7]{3}$")
MAX_OUTPUT = 64 * 1024  # 출력이 아주 커도 메모리를 다 쓰지 않게 자른다


@dataclass(frozen=True)
class SshConnection:
    host: str
    key_path: Path
    user: str = "deploy"
    port: int = 22

    def __post_init__(self):
        if not _HOST.match(self.host) or not _USER.match(self.user) or not 1 <= self.port <= 65535:
            raise ValueError("invalid ssh connection")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def connection_failed(self) -> bool:
        # ssh는 접속 자체가 실패하면 255로 끝난다(원격 명령이 255를 돌려줘도 같다).
        return self.returncode == 255


def _text(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    return text[:MAX_OUTPUT]


def safe_env() -> dict[str, str]:
    # 서비스 서버의 AWS 자격 증명이나 토큰 같은 환경변수가 ssh 프로세스로 넘어가지 않게 한다.
    return {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}


class SshRunner:
    def __init__(
        self,
        connection: SshConnection,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        connect_timeout: int = 10,
        known_hosts: Path | None = None,
    ):
        self.connection = connection
        self._runner = runner  # 시험에서는 가짜 실행기로 바꿔 끼운다
        self._popen = popen
        self._connect_timeout = connect_timeout
        self._known_hosts = known_hosts

    def _command(self, remote: Sequence[str]) -> list[str]:
        conn = self.connection
        options = [
            "-i",
            str(conn.key_path),
            "-p",
            str(conn.port),
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            f"ConnectTimeout={self._connect_timeout}",
            "-o",
            "ServerAliveInterval=15",
            # 처음 접속한 서버의 키는 기록해 두고 이후 바뀌면 거부한다(첫 접속만 신뢰).
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]
        if self._known_hosts:
            options += ["-o", f"UserKnownHostsFile={self._known_hosts}"]
        return ["ssh", *options, f"{conn.user}@{conn.host}", shlex.join(remote)]

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: float = 60,
        input: str | bytes | None = None,
        stdin: IO[bytes] | None = None,
    ) -> CommandResult:
        """원격에서 명령을 실행한다. 실패는 예외가 아니라 결과의 returncode로 알린다.

        input은 작은 내용을 통째로, stdin은 큰 내용(이미지 등)을 스트림으로 흘려보낼 때 쓴다."""
        if (
            not args
            or any("\0" in arg for arg in args)
            or (input is not None and stdin is not None)
        ):
            raise ValueError("invalid remote command")
        data = input.encode() if isinstance(input, str) else input
        feed = {"stdin": stdin} if stdin is not None else {"input": data}
        try:
            done = self._runner(
                self._command(args),
                capture_output=True,
                timeout=timeout,
                env=safe_env(),
                check=False,
                **feed,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(124, _text(exc.stdout), _text(exc.stderr), timed_out=True)
        return CommandResult(done.returncode, _text(done.stdout), _text(done.stderr))

    def popen(self, args: Sequence[str]) -> subprocess.Popen:
        """원격 명령을 시작하고 표준 출력과 오류를 파이프로 돌려준다. 아주 큰 출력(DB 덤프)을 메모리에 모으지 않고
        다른 서버의 표준 입력으로 그대로 흘려보낼 때 쓴다. 기다리고 정리하는 것은 호출하는 쪽의 몫이다."""
        if not args or any("\0" in arg for arg in args):
            raise ValueError("invalid remote command")
        return self._popen(
            self._command(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=safe_env(),
        )

    def put(
        self, remote_path: str, data: str | bytes, *, mode: str = "0644", timeout: float = 30
    ) -> CommandResult:
        """원격에 파일을 쓴다. 비밀 파일이 잠깐이라도 다른 사람에게 읽히지 않게 비공개로 만든 뒤
        권한을 맞추고, 임시 파일을 옮겨서 한 번에 교체한다."""
        if not _REMOTE_PATH.match(remote_path) or ".." in remote_path or not _MODE.match(mode):
            raise ValueError("invalid remote path or mode")
        script = 'umask 077 && cat > "$1.tmp" && chmod "$2" "$1.tmp" && mv "$1.tmp" "$1"'
        return self.run(["sh", "-c", script, "sh", remote_path, mode], input=data, timeout=timeout)

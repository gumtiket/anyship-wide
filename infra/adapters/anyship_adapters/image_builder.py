"""서비스 서버의 로컬 도커에서 `<앱>:<커밋 SHA>` 이미지를 빌드한다.

서비스(A)가 PR이 머지된 커밋의 소스를 서비스 서버의 로컬 폴더로 준비해서 부르고, 만들어진 이미지는 어댑터가
`docker save | ssh docker load`로 호스트에 보낸다(레지스트리 없음). Dockerfile은 B가 만들어 레포에 넣어 둔 것을 쓴다.

주의: 사용자 레포의 Dockerfile(임의의 RUN 명령)을 **서비스 서버에서** 실행한다. 이 서버의 IAM 역할은 사용자 계정
AssumeRole 권한을 가진다. 격리된 빌드 환경은 확장 항목이라, 지금은 샘플 레포에만 쓴다는 전제다.

지키는 것:
  * 원본 폴더를 건드리지 않고 임시 복사본에서 빌드한다. `.git`과 `.env*`는 복사하지 않고, 심볼릭 링크는 따라가지 않는다
    (레포 안의 링크로 서버의 다른 파일이 이미지에 들어가는 일을 막는다).
  * 도커 프로세스에는 서비스 서버의 AWS 환경과 토큰을 넘기지 않고, 입력을 기다리지 못하게 한다.
  * 이미지 아키텍처는 linux/amd64로 고정하고 빌드 뒤에 확인한다.
  * 같은 (앱, SHA)를 동시에 빌드하지 않고, 제한 시간이 지나면 프로세스를 종료한다.

실패는 BuildError(AdapterError 포함)로 던진다. 호출하는 쪽이 결과의 오류로 바꾼다.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .base import LogFn
from .models import APP_NAME_PATTERN, IMAGE_TAG_PATTERN, AdapterError, LogEvent
from .redact import redact_text
from .ssh import safe_env

_APP = re.compile(APP_NAME_PATTERN)
_TAG = re.compile(IMAGE_TAG_PATTERN)
MAX_CONTEXT_BYTES = 200 * 1024 * 1024  # 이보다 큰 소스는 빌드하지 않는다(서비스 서버 디스크 보호)
MAX_LINE = 1000
TAIL_LINES = 15
_EXCLUDED = (".git", ".env", ".env.*")
_ACTIVE: set[tuple[str, str]] = set()  # 지금 빌드 중인 (앱, SHA)
_GUARD = threading.Lock()


class BuildError(Exception):
    """빌드할 수 없거나 실패했을 때 던진다. tail은 비밀을 가린 마지막 출력(결과의 details용)이다."""

    def __init__(self, error: AdapterError, tail: str = ""):
        super().__init__(error.message)
        self.error, self.tail = error, tail


def _fail(
    code: str, message: str, hint: str | None = None, retryable: bool = False, tail: str = ""
) -> BuildError:
    return BuildError(
        AdapterError(code=code, message=message, hint=hint, retryable=retryable), tail
    )


@dataclass(frozen=True)
class BuiltImage:
    image: str  # `<앱>:<SHA>`. 어댑터의 deploy가 이 이미지를 보낸다
    image_id: str


class ImageBuilder:
    def __init__(
        self,
        *,
        binary: str = "docker",
        timeout: float = 600,
        popen=subprocess.Popen,
        run=subprocess.run,
    ):
        self._binary, self._timeout, self._popen, self._run = (
            binary,
            timeout,
            popen,
            run,
        )  # 시험에서는 가짜로 바꿔 끼운다

    def build(
        self,
        source_dir: Path,
        app: str,
        commit_sha: str,
        log: LogFn,
        *,
        dockerfile: str = "Dockerfile",
    ) -> BuiltImage:
        source = Path(source_dir)
        if not _APP.match(app) or not _TAG.match(commit_sha):
            raise _fail(
                "invalid_build_input",
                "앱 이름이나 커밋 SHA 형식이 올바르지 않습니다.",
                hint="앱 이름은 소문자로 시작하는 3~63자, SHA는 16진수 7~40자여야 합니다.",
            )
        image = f"{app}:{commit_sha}"
        relative = self._check_source(source, dockerfile)
        with (
            self._exclusive(app, commit_sha),
            tempfile.TemporaryDirectory(prefix="anyship-build-") as work,
        ):
            log(
                LogEvent(
                    step=1,
                    total=2,
                    name="빌드 준비",
                    message="소스를 임시 폴더로 복사하는 중(.git과 .env*는 제외)",
                )
            )
            context = self._copy_context(source, Path(work))
            self._build(context, relative, image, log)
        return BuiltImage(image=image, image_id=self._inspect(image))

    def prune(self, app: str, keep: int = 5) -> list[str]:
        """앱의 이미지 중 가장 최근 keep개만 남기고 지운다. 지운 이미지 이름을 돌려준다(쓰이는 중이면 건너뜀)."""
        if not _APP.match(app) or keep < 1:
            raise ValueError("invalid prune arguments")
        listing = self._run(
            [
                self._binary,
                "image",
                "ls",
                "--filter",
                f"reference={app}",
                "--format",
                "{{.Tag}}\t{{.CreatedAt}}",
            ],
            capture_output=True,
            text=True,
            env=safe_env(),
            timeout=60,
        )
        rows = [line.split("\t", 1) for line in listing.stdout.splitlines() if "\t" in line]
        tags = [(created, tag) for tag, created in rows if _TAG.match(tag)]
        removed = []
        for _, tag in sorted(tags, reverse=True)[keep:]:
            done = self._run(
                [self._binary, "image", "rm", f"{app}:{tag}"],
                capture_output=True,
                text=True,
                env=safe_env(),
                timeout=60,
            )
            if done.returncode == 0:
                removed.append(f"{app}:{tag}")
        return removed

    # -- 내부 ---------------------------------------------------------------------------------
    @contextmanager
    def _exclusive(self, app: str, sha: str) -> Iterator[None]:
        with _GUARD:
            if (app, sha) in _ACTIVE:
                raise _fail(
                    "build_in_progress",
                    "같은 앱과 커밋의 빌드가 이미 진행 중입니다.",
                    retryable=True,
                )
            _ACTIVE.add((app, sha))
        try:
            yield
        finally:
            with _GUARD:
                _ACTIVE.discard((app, sha))

    def _check_source(self, source: Path, dockerfile: str) -> str:
        path = PurePosixPath(dockerfile)
        if (
            not source.is_dir()
            or not dockerfile
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in dockerfile
        ):
            raise _fail("invalid_build_input", "소스 폴더나 Dockerfile 경로가 올바르지 않습니다.")
        target = source / path
        try:  # Dockerfile이 링크이거나 소스 폴더 밖을 가리키면 거부한다
            inside = (
                target.is_file()
                and not target.is_symlink()
                and target.resolve().is_relative_to(source.resolve())
            )
        except OSError:
            inside = False
        if not inside:
            raise _fail(
                "invalid_build_input",
                "소스 폴더 안에서 Dockerfile을 찾지 못했습니다.",
                hint=f"경로: {dockerfile}",
            )
        return str(path)

    def _copy_context(self, source: Path, work: Path) -> Path:
        ignore, size = shutil.ignore_patterns(*_EXCLUDED), 0
        for root, dirs, files in os.walk(source):  # 링크를 따라가지 않는다
            skip = set(ignore(root, dirs + files))
            dirs[:] = [d for d in dirs if d not in skip]
            for name in files:
                if name not in skip:
                    size += (Path(root) / name).lstat().st_size
            if size > MAX_CONTEXT_BYTES:
                raise _fail(
                    "build_context_too_large",
                    "빌드할 소스가 너무 큽니다.",
                    hint=f"{MAX_CONTEXT_BYTES // (1024 * 1024)}MB 이하여야 합니다. .dockerignore가 아니라 소스 자체를 줄여 주세요.",
                )
        return Path(shutil.copytree(source, work / "context", symlinks=True, ignore=ignore))

    def _build(self, context: Path, dockerfile: str, image: str, log: LogFn) -> None:
        args = [
            self._binary,
            "build",
            "--platform",
            "linux/amd64",
            "--tag",
            image,
            "--file",
            str(context / dockerfile),
            str(context),
        ]
        log(LogEvent(step=2, total=2, name="이미지 빌드", message=f"이미지 {image} 빌드 시작"))
        try:
            child = self._popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=safe_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            raise _fail("docker_not_found", "서비스 서버에서 docker를 찾을 수 없습니다.") from None
        expired, tail = [], deque(maxlen=TAIL_LINES)

        def expire():
            expired.append(True)
            child.kill()

        timer = threading.Timer(self._timeout, expire)
        timer.start()
        try:
            for raw in child.stdout:
                line = redact_text(raw.rstrip()[:MAX_LINE])
                if line:
                    tail.append(line)
                    log(LogEvent(step=2, total=2, name="이미지 빌드", message=line))
            code = child.wait()
        except (
            BaseException
        ):  # 로그 함수가 예외를 던지거나 작업이 취소되면 도커 클라이언트를 남겨 두지 않는다
            child.kill()
            child.wait()
            raise
        finally:
            timer.cancel()
        text = "\n".join(tail)
        if expired:
            raise _fail(
                "build_timeout",
                "이미지 빌드가 제한 시간 안에 끝나지 않아 중단했습니다.",
                hint="캐시가 쌓이면 다음 빌드는 더 빠릅니다. 다시 시도해 주세요.",
                retryable=True,
                tail=text,
            )
        if code != 0:
            if (
                "Cannot connect to the Docker daemon" in text
                or "permission denied while trying to connect" in text
            ):
                raise _fail(
                    "docker_unavailable",
                    "도커 데몬에 연결할 수 없습니다.",
                    hint="서비스 서버에서 도커가 실행 중이고 서비스 사용자가 docker 그룹에 속하는지 확인해 주세요.",
                    retryable=True,
                    tail=text,
                )
            raise _fail(
                "docker_build_failed",
                "이미지 빌드가 실패했습니다.",
                hint="Dockerfile과 의존성 설치 단계를 확인해 주세요.",
                tail=text,
            )

    def _inspect(self, image: str) -> str:
        done = self._run(
            [self._binary, "image", "inspect", "--format", "{{.Id}} {{.Architecture}}", image],
            capture_output=True,
            text=True,
            env=safe_env(),
            timeout=60,
        )
        parts = done.stdout.split()
        if done.returncode != 0 or len(parts) != 2:
            raise _fail("docker_build_failed", "빌드한 이미지를 확인하지 못했습니다.")
        if parts[1] != "amd64":
            raise _fail("wrong_architecture", f"이미지 아키텍처가 amd64가 아닙니다({parts[1]}).")
        return parts[0]

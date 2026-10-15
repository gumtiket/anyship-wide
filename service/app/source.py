"""배포할 소스를 가져오는 부품. 두 가지 구현이 같은 `SourceProvider` 자리에 들어간다.

- `GitHubCloneSource`(기본): 사용자가 연결한 저장소의 브랜치를 **사용자 토큰으로 GitHub에서 받는다**(얕은 복제). 서비스가 직접 받은 저장소라
  `.git/config`는 clone이 만든 것이고 저장소 내용이 정할 수 없다. 커밋 SHA는 받은 커밋의 앞 12자리다.
- `LocalFolderSource`(시험용): 서비스 서버의 폴더에 이미 받아 둔 소스를 쓴다.

소스 루트에 `Dockerfile`과 `deploy-spec.yaml`이 있어야 한다(AI가 서비스에 연결되기 전의 가정). 소스 폴더는 읽기만 하고 수정하지 않는다. 빌더가
임시 복사본에서 빌드한다. 받은 소스를 쓴 뒤에는 `SourceResult.cleanup()`으로 지운다.

`LocalFolderSource`의 커밋 SHA는 파일 내용으로 정한다. 사용자가 둔 폴더에서 `git`을 실행하지 않는다(그 폴더의 `.git/config`가 명령을 실행하게
만들 수 있고, 커밋하지 않은 수정이 있으면 HEAD가 실제 내용을 말해 주지 못한다). 이미지 빌더가 복사하지 않는 파일(`.git`, `.env*`)은 해시에도 넣지 않는다.
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from github.repository import clone_repository, run_git

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_FILES = ("Dockerfile", "deploy-spec.yaml")
MAX_BYTES = 200 * 1024 * 1024  # 이미지 빌더의 소스 한도와 같다
MAX_ENTRIES = 20000
MAX_SPEC_BYTES = 64 * 1024
_SKIP_DIRS = {".git", "__pycache__"}


class SourceError(Exception):
    """소스를 쓸 수 없을 때. 문구에는 서버의 경로를 싣지 않는다."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class SourceResult:
    path: Path  # 소스 폴더(읽기 전용으로 취급한다)
    commit_sha: str  # 7~40자리 16진수. 이미지 태그가 된다
    spec: Mapping[str, Any]  # deploy-spec.yaml의 내용(형식 검증은 Deployer가 한다)
    cleanup: Callable[[], None] | None = (
        None  # 받아 둔 임시 소스를 지운다(배포가 끝났거나 쓰지 않게 되었을 때)
    )


class SourceProvider(Protocol):
    def fetch(self, full_name: str, branch: str = "", token: str | None = None) -> SourceResult: ...


class LocalFolderSource:
    """서버의 폴더에 이미 받아 둔 소스. 브랜치와 토큰은 쓰지 않는다."""

    def __init__(self, root: Path):
        self._root = Path(root)

    def fetch(self, full_name: str, branch: str = "", token: str | None = None) -> SourceResult:
        name = full_name.rsplit("/", 1)[-1]
        if not _NAME.fullmatch(name) or name in (".", ".."):
            raise SourceError("source_not_found", "이 저장소의 소스 폴더를 찾을 수 없습니다.")
        root = self._root.resolve()
        folder = root / name
        if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != root:
            raise SourceError("source_not_found", "이 저장소의 소스 폴더를 찾을 수 없습니다.")
        _require_files(folder)
        return SourceResult(folder, _fingerprint(folder), _read_spec(folder / "deploy-spec.yaml"))


class GitHubCloneSource:
    """연결한 저장소의 브랜치를 사용자 토큰으로 GitHub에서 받는다(얕은 복제 한 커밋)."""

    def __init__(self, workspace: Path, *, clone=clone_repository, revision=None):
        self._workspace = Path(workspace)
        self._clone = clone  # 시험에서는 가짜로 바꿔 끼운다
        self._revision = revision or (lambda folder: run_git(["rev-parse", "HEAD"], cwd=folder))

    def clear_stale(self) -> None:
        """서버가 비정상으로 멈추면서 남은 임시 소스를 지운다. 프로세스당 하나의 워커만 있으므로 쓰는 중인 것은 없다."""
        if self._workspace.is_dir():
            for child in self._workspace.iterdir():
                shutil.rmtree(child, ignore_errors=True)

    def fetch(self, full_name: str, branch: str = "", token: str | None = None) -> SourceResult:
        if not _REPOSITORY.fullmatch(full_name) or any(
            part in (".", "..") for part in full_name.split("/")
        ):
            raise SourceError("source_not_found", "저장소 이름이 올바르지 않습니다.")
        if (
            not _BRANCH.fullmatch(branch)
            or branch.startswith(("-", "/"))
            or ".." in branch
            or branch.endswith(("/", ".lock"))
        ):
            raise SourceError("source_not_found", "브랜치 이름이 올바르지 않습니다.")
        if not token:
            raise SourceError(
                "github_token_missing",
                "GitHub 인증 정보가 없어 소스를 받을 수 없습니다. 다시 로그인해 주세요.",
            )
        self._workspace.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="src-", dir=self._workspace))
        destination = root / full_name.split("/")[1]

        def cleanup() -> None:
            shutil.rmtree(root, ignore_errors=True)

        try:
            try:
                self._clone(full_name, branch, destination, token, depth=1)
                revision = self._revision(destination)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                # git의 오류 출력은 싣지 않는다(저장소 주소와 인증 관련 정보가 섞일 수 있다).
                raise SourceError(
                    "source_fetch_failed", "GitHub에서 소스를 받지 못했습니다."
                ) from None
            if not isinstance(revision, str) or not _COMMIT.fullmatch(revision):
                raise SourceError("source_fetch_failed", "받은 소스의 커밋을 확인하지 못했습니다.")
            _require_files(destination)
            _fingerprint(destination)  # 파일 수와 크기 제한을 확인한다(값은 쓰지 않는다)
            return SourceResult(
                destination, revision[:12], _read_spec(destination / "deploy-spec.yaml"), cleanup
            )
        except BaseException:
            cleanup()
            raise


def _require_files(folder: Path) -> None:
    for required in REQUIRED_FILES:
        entry = folder / required
        if entry.is_symlink() or not entry.is_file():
            raise SourceError(
                "source_incomplete", f"소스 폴더의 루트에 {required} 파일이 필요합니다."
            )


def _read_spec(path: Path) -> Mapping[str, Any]:
    if path.stat().st_size > MAX_SPEC_BYTES:
        raise SourceError("source_incomplete", "deploy-spec.yaml이 너무 큽니다.")
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError):
        raise SourceError("source_incomplete", "deploy-spec.yaml을 읽을 수 없습니다.") from None
    if not isinstance(spec, dict):
        raise SourceError(
            "source_incomplete", "deploy-spec.yaml은 항목들의 목록이 아니라 매핑이어야 합니다."
        )
    return spec


def _fingerprint(folder: Path) -> str:
    """경로 순서가 정해진 해시. 내용, 실행 권한, 링크 대상이 바뀌면 달라진다. 링크는 따라가지 않는다."""
    digest, total, count = hashlib.sha256(), 0, 0
    for directory, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".env"))
        for entry in sorted(
            files + [d for d in dirs if os.path.islink(os.path.join(directory, d))]
        ):
            if entry.startswith(".env"):
                continue
            path = os.path.join(directory, entry)
            relative = os.path.relpath(path, folder).replace(os.sep, "/")
            count += 1
            if count > MAX_ENTRIES:
                raise SourceError("source_too_large", "소스의 파일이 너무 많습니다.")
            if os.path.islink(path):
                digest.update(
                    b"L\0" + relative.encode() + b"\0" + os.readlink(path).encode() + b"\0"
                )
                continue
            info = os.stat(path)
            total += info.st_size
            if total > MAX_BYTES:
                raise SourceError("source_too_large", "소스가 너무 큽니다.")
            digest.update(
                b"F\0" + relative.encode() + b"\0" + (b"x" if info.st_mode & 0o111 else b"-")
            )
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()[:12]

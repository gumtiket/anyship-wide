import difflib
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from ai.detectors.repo import RepoView


def source_files(repo: RepoView) -> dict[str, str]:
    return {name: repo.read(name) for name in repo.files() if not name.startswith(".git")}


def make_diff(before: dict[str, str], after: dict[str, str]) -> str:
    chunks = []
    for name in sorted(set(before) | set(after)):
        if before.get(name) == after.get(name):
            continue
        for line in difflib.unified_diff(
            before.get(name, "").splitlines(keepends=True),
            after.get(name, "").splitlines(keepends=True),
            fromfile=f"a/{name}" if name in before else "/dev/null",
            tofile=f"b/{name}" if name in after else "/dev/null",
        ):
            if line.endswith("\n"):
                chunks.append(line)
            else:
                chunks.append(line + "\n\\ No newline at end of file\n")
    return "".join(chunks)


def patch_paths(diff: str, allowed: set[str]) -> set[str]:
    if not diff.strip():
        return set()
    paths = set()
    old = None
    saw_hunk = False
    for line in diff.splitlines():
        if line.startswith(("diff --git ", "index ")):
            continue
        if line.startswith(
            (
                "GIT binary patch",
                "Binary files",
                "rename ",
                "old mode ",
                "new mode ",
                "new file mode ",
                "deleted file mode ",
            )
        ):
            raise ValueError("patch_metadata_forbidden")
        if line.startswith(("--- ", "+++ ")):
            raw = line[4:].split("\t")[0]
            if raw == "/dev/null":
                name = None
            else:
                prefix = "a/" if line.startswith("--- ") else "b/"
                if not raw.startswith(prefix):
                    raise ValueError("patch_path_prefix")
                name = raw[2:]
                path = PurePosixPath(name)
                if (
                    not name
                    or path.is_absolute()
                    or ".." in path.parts
                    or "." in path.parts
                    or "\\" in name
                    or name not in allowed
                ):
                    raise ValueError("patch_path_not_allowed")
                paths.add(name)
            if line.startswith("--- "):
                old = name
            elif name is None or (old is not None and old != name):
                raise ValueError("patch_delete_or_rename_forbidden")
        elif line.startswith("@@ "):
            saw_hunk = True
        elif line and line[0] not in {" ", "+", "-", "\\"}:
            raise ValueError("patch_non_diff_content")
    if not paths or not saw_hunk:
        raise ValueError("patch_missing_hunk")
    return paths


class Workspace:
    def __init__(self, files: dict[str, str]) -> None:
        self._temporary = TemporaryDirectory(prefix="bronze-transform-")
        self.root = Path(self._temporary.name)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_ATTR_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
        )
        for name, source in files.items():
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.encode("utf-8"))
        self.git("init", "--quiet", "--template=")

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *args) -> None:
        self._temporary.cleanup()

    def git(self, *args: str, diff: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.root,
                env=self.env,
                input=diff.encode("utf-8") if diff is not None else None,
                capture_output=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("git_timeout") from None

    def apply(self, diff: str) -> None:
        if not diff:
            return
        if self.git("apply", "--check", "-", diff=diff).returncode:
            raise ValueError("git_apply_check_failed")
        if self.git("apply", "-", diff=diff).returncode:
            raise ValueError("git_apply_failed")

    def compile(self) -> bool:
        # compileall은 소스를 파싱만 하고 앱을 import·실행하지 않는다. 출력은 비공개로 둔다.
        try:
            return (
                subprocess.run(
                    [sys.executable, "-m", "compileall", "-q", str(self.root)],
                    capture_output=True,
                    timeout=20,
                ).returncode
                == 0
            )
        except subprocess.TimeoutExpired:
            return False

    def read(self, names: set[str]) -> dict[str, str]:
        return {name: (self.root / name).read_bytes().decode("utf-8") for name in sorted(names)}

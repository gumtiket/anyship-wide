import ast
import os
from pathlib import Path

from pathspec import GitIgnoreSpec

from ai.models import WarningItem
from ai.source_names import aliases as aliases
from ai.source_names import qualified as qualified

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".aws",
        ".ssh",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        "build",
        "dist",
        "out",
        "output",
    }
)


class RepoView:
    """크기를 제한한 결정적 읽기 전용 보기. 저장소 모듈을 import하지 않는다."""

    def __init__(self, root: str | Path, *, max_files: int = 2000, max_bytes: int = 524288) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("레포 디렉터리가 존재하지 않습니다.")
        self.warnings: list[WarningItem] = []
        self._texts: dict[str, str] = {}
        self._trees: dict[str, ast.Module] = {}
        filters: dict[Path, GitIgnoreSpec] = {}
        candidates = 0

        def ignored(path: Path, is_dir: bool = False) -> bool:
            decision = False
            for scope, spec in filters.items():
                if path.is_relative_to(scope):
                    relative = path.relative_to(scope).as_posix() + ("/" if is_dir else "")
                    match = spec.check_file(relative).include
                    if match is not None:
                        decision = match
            return decision

        for directory, dirs, names in os.walk(self.root, followlinks=False):
            current = Path(directory)
            ignore_file = current / ".gitignore"
            if ignore_file.is_file() and not ignore_file.is_symlink():
                try:
                    filters[current] = GitIgnoreSpec.from_lines(
                        ignore_file.read_text().splitlines()
                    )
                except (OSError, UnicodeError):
                    self.warnings.append(
                        WarningItem(
                            code="gitignore_unreadable",
                            message="일부 .gitignore를 읽지 못했습니다.",
                        )
                    )
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in EXCLUDED_DIRS
                and not (current / d).is_symlink()
                and not ignored(current / d, True)
            )
            for name in sorted(names):
                path = current / name
                if name.lower().endswith(
                    (
                        ".db",
                        ".db-wal",
                        ".db-shm",
                        ".db-journal",
                        ".sqlite",
                        ".sqlite3",
                        ".sqlite-wal",
                        ".sqlite-shm",
                        ".sqlite3-wal",
                        ".sqlite3-shm",
                        ".log",
                    )
                ):
                    continue  # 실행 중 생기는 데이터는 소스 입력도, 제안할 빌드 입력도 아니다.
                if path.is_symlink() or ignored(path) or name.lower().startswith(".env"):
                    continue
                if name == "VIOLATIONS.json" or name.endswith(".expected.json"):
                    continue  # 정답 데이터는 분석 입력이 아니다.
                candidates += 1
                if candidates > max_files:
                    self.warnings.append(
                        WarningItem(
                            code="scan_limit",
                            message="파일 수 상한으로 분석이 일부 생략되었습니다.",
                        )
                    )
                    return
                try:
                    if not path.is_file() or path.stat().st_size > max_bytes:
                        continue
                    content = path.read_bytes()
                    if b"\0" in content:
                        continue
                    text = content.decode("utf-8")
                except (OSError, UnicodeError):
                    continue
                relative = path.relative_to(self.root).as_posix()
                self._texts[relative] = text
                if path.suffix == ".py":
                    try:
                        self._trees[relative] = ast.parse(text, filename=relative)
                    except SyntaxError:
                        self.warnings.append(
                            WarningItem(
                                code="python_syntax_error",
                                message=f"{relative}: 구문 분석 실패. 원문은 출력하지 않습니다.",
                            )
                        )

    def files(self) -> tuple[str, ...]:
        return tuple(sorted(self._texts))

    def read(self, relative: str) -> str:
        return self._texts[relative]

    def modules(self) -> list[tuple[str, ast.Module]]:
        return sorted(self._trees.items())

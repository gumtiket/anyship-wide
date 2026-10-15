"""Deterministic local-only stand-in for AI modification. Never contacts GitHub."""

import ast
import hashlib
import os
import subprocess
import uuid
from pathlib import Path

FILE_NAME = "ai_analysis_placeholder.py"
CONTENT = (
    "# AI 분석 모듈이 아직 연결되지 않았습니다.\n"
    "# 테스트 모드에서 코드 수정과 PR 검토 흐름을 확인하기 위한 임시 파일입니다.\n"
    "# 실제 AI 분석 결과가 아니며, AI 모듈 연결 후 제거할 수 있습니다.\n"
)


class DemoFailure(Exception):
    pass


def workspace(root: Path, change_id: str) -> Path:
    key = str(uuid.UUID(change_id))
    path = root.resolve() / key
    if path.resolve().parent != root.resolve() or path.is_symlink():
        raise DemoFailure("데모 작업 경로를 확인할 수 없습니다.")
    return path


def git(path, *args):
    # Don't inherit repository, credentials, hooks, filters or signing configuration.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=" + str(path / ".disabled-hooks"),
                "-c",
                "commit.gpgSign=false",
                "-c",
                "core.autocrlf=false",
                "-c",
                "user.name=Demo User",
                "-c",
                "user.email=demo@example.com",
                *args,
            ],
            cwd=path,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
            timeout=20,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise DemoFailure(
            "로컬 Git 작업에 실패했습니다. Git 설치와 작업 폴더 상태를 확인해 주세요."
        ) from None


def apply(root, change_id, content):
    path = workspace(root, change_id)
    if not path.exists():
        path.mkdir(parents=True)
        git(path, "init", "--initial-branch=main")
        (path / "README.md").write_text(
            "# Local demo fixture\n\nThis repository is an offline sample, not a clone of a GitHub repository.\n",
            encoding="utf-8",
        )
        git(path, "add", "README.md")
        git(path, "commit", "-m", "chore: initialize local demo fixture")
        git(path, "checkout", "-b", f"demo/placeholder-{change_id[:8]}")
    if git(path, "branch", "--show-current") != f"demo/placeholder-{change_id[:8]}":
        raise DemoFailure("데모 작업 브랜치가 일치하지 않습니다.")
    target = path / FILE_NAME
    if target.is_symlink():
        raise DemoFailure("데모 파일 경로가 올바르지 않습니다.")
    if content != CONTENT:
        raise DemoFailure("데모 변경 내용이 예시와 일치하지 않습니다.")
    ast.parse(content)  # Syntax validation only; never execute even this fixture.
    target.write_text(content, encoding="utf-8", newline="\n")
    git(path, "add", "--", FILE_NAME)
    diff = git(path, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--", FILE_NAME)
    if not diff:
        raise DemoFailure("검토할 변경 내용이 없습니다.")
    return diff, hashlib.sha256(diff.encode()).hexdigest()


def commit(root, change_id, expected_diff, expected_content):
    path = workspace(root, change_id)
    if not path.is_dir() or (path / FILE_NAME).is_symlink():
        raise DemoFailure("데모 작업 폴더를 찾을 수 없습니다.")
    if git(path, "branch", "--show-current") != f"demo/placeholder-{change_id[:8]}":
        raise DemoFailure("데모 작업 브랜치가 일치하지 않습니다.")
    if (path / FILE_NAME).read_text(encoding="utf-8") != expected_content:
        raise DemoFailure("검토 이후 파일 내용이 변경되었습니다.")
    # Compare the full stage, so unreviewed staged files cannot enter the commit.
    staged = git(path, "diff", "--cached", "--no-ext-diff", "--no-textconv")
    if staged != expected_diff:
        raise DemoFailure("검토 이후 변경 내용이 달라졌습니다. 다시 확인해 주세요.")
    git(path, "commit", "-m", "chore: add AI module placeholder (demo only)")
    return git(path, "rev-parse", "HEAD")

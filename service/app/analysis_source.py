"""Bounded Git tree/blob snapshots. Never execute repository configuration or code."""

import base64
import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_FILES = 2000
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
SHA = re.compile(r"^[0-9a-f]{40}$")
EXCLUDED = {
    ".git",
    ".github",
    ".aws",
    ".ssh",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
}
DATA_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".p12", ".pfx"}


class AnalysisError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def safe_path(name):
    if not isinstance(name, str) or not name or len(name) > 240:
        return False
    parts = name.split("/")
    return not PurePosixPath(name).is_absolute() and all(
        part not in ("", ".", "..")
        and not part.endswith((" ", "."))
        and not re.search(r'[\\:\x00-\x1f\x7f<>"|?*]', part)
        and part.split(".")[0].upper()
        not in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(10)),
            *(f"LPT{i}" for i in range(10)),
        }
        for part in parts
    )


def excluded(name):
    return (
        any(p.lower() in EXCLUDED or p.lower().startswith(".env") for p in name.split("/"))
        or Path(name).suffix.lower() in DATA_SUFFIXES
    )


def blob_sha(content):
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


@dataclass
class Snapshot:
    base_sha: str
    base_tree: str
    files: dict
    entries: dict
    skipped: int = 0


def fetch_snapshot(
    github, token, repository, base_sha, root, *, cancelled=lambda: False, timeout=120
):
    started = time.monotonic()

    def check():
        if cancelled() or time.monotonic() - started > timeout:
            raise AnalysisError(
                "source_timeout", "소스 준비 시간이 초과되거나 작업이 중단됐습니다."
            )

    if not SHA.fullmatch(base_sha):
        raise AnalysisError("invalid_sha", "기준 커밋을 확인할 수 없습니다.")
    commit = github.git_commit(token, repository, base_sha)
    tree_sha = commit.get("tree", {}).get("sha", "")
    if not SHA.fullmatch(tree_sha) or commit.get("sha") != base_sha:
        raise AnalysisError("invalid_snapshot", "기준 커밋과 소스가 일치하지 않습니다.")
    tree = github.recursive_tree(token, repository, tree_sha)
    entries = tree.get("tree", [])
    if tree.get("truncated") or len(entries) > MAX_FILES:
        raise AnalysisError("source_too_large", "분석 가능한 파일 수를 초과했습니다.")
    files, metadata, seen, total, skipped = {}, {}, set(), 0, 0
    for entry in entries:
        check()
        name = entry.get("path")
        if not safe_path(name) or name.casefold() in seen:
            raise AnalysisError("unsafe_source_path", "지원하지 않거나 중복된 소스 경로입니다.")
        seen.add(name.casefold())
        if entry.get("type") != "tree":
            metadata[name] = {"sha": entry.get("sha"), "mode": entry.get("mode")}
        if excluded(name):
            skipped += 1
            continue
        if entry.get("type") == "tree":
            continue
        if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
            raise AnalysisError(
                "unsafe_source_type", "심볼릭 링크·서브모듈은 분석 대상에서 지원하지 않습니다."
            )
        size, sha = entry.get("size"), entry.get("sha", "")
        if type(size) is not int or size < 0 or size > MAX_FILE_BYTES or not SHA.fullmatch(sha):
            raise AnalysisError("source_too_large", "파일 크기 또는 소스 식별자를 확인해 주세요.")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise AnalysisError("source_too_large", "분석 가능한 소스 크기를 초과했습니다.")
        metadata[name] = {"sha": sha, "mode": entry["mode"]}
        blob = github.blob(token, repository, sha)
        try:
            if (
                blob.get("encoding") != "base64"
                or len(blob.get("content", "")) > MAX_FILE_BYTES * 2
            ):
                raise ValueError()
            raw = base64.b64decode("".join(blob["content"].split()), validate=True)
            if len(raw) != size or blob_sha(raw) != sha:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise AnalysisError("invalid_blob", "소스 파일 무결성 확인에 실패했습니다.") from None
        try:
            content = raw.decode("utf-8")
            if "\0" in content:
                raise UnicodeError()
        except UnicodeError:
            skipped += 1
            continue
        files[name] = content
    check()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    return Snapshot(base_sha, tree_sha, files, metadata, skipped)

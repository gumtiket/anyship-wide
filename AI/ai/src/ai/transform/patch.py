"""LLM이 처음 낸 hunk를 고유한 문맥으로 적용한 뒤, 정규화된 Git diff를 만든다.

이 모듈은 전달 형식만 바로잡을 뿐 승인하지 않는다. 호출한 쪽이 원본을 기준으로
비밀, 의미, 컴파일, 신호, 선택된 AST 범위를 따로 검증해야 한다.
"""

import re
from difflib import SequenceMatcher

from ai.transform.workspace import make_diff, patch_paths

MARKER = "[REDACTED]"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@.*$")


def _hunks(diff: str, allowed: set[str]) -> dict[str, list[tuple[list[str], list[str]]]]:
    lines = diff.splitlines(keepends=True)
    # 허용된 이름과 정확히 같을 때만 접두사를 붙인다. 경로 탈출·이름 변경·삭제는 거부한다.
    for index, line in enumerate(lines):
        if line.startswith(("--- ", "+++ ")):
            name = line[4:].rstrip("\r\n")
            if name in allowed:
                prefix = "a/" if line.startswith("--- ") else "b/"
                lines[index] = line[:4] + prefix + name + "\n"
    patch_paths("".join(lines), allowed)
    result: dict[str, list[tuple[list[str], list[str]]]] = {}
    path = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(("diff --git ", "index ")):
            index += 1
            continue
        if line.startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise ValueError("patch_invalid_hunk")
            path = line[6:].rstrip("\r\n").split("\t")[0]
            if path not in allowed or lines[index + 1][6:].rstrip("\r\n").split("\t")[0] != path:
                raise ValueError("patch_path_not_allowed")
            if path in result:
                raise ValueError("patch_duplicate_file")
            result[path] = []
            index += 2
            continue
        if path is None or not HUNK.fullmatch(line.rstrip("\r\n")):
            raise ValueError("patch_invalid_hunk")
        index += 1
        old, new = [], []
        previous = None
        changed = False
        while index < len(lines):
            line = lines[index]
            if line.startswith(("@@ ", "--- ", "diff --git ", "index ")):
                break
            if line.rstrip("\r\n") == "\\ No newline at end of file":
                if previous not in {" ", "-", "+"}:
                    raise ValueError("patch_invalid_hunk")
                if previous in {" ", "-"}:
                    old[-1] = old[-1].rstrip("\r\n")
                if previous in {" ", "+"}:
                    new[-1] = new[-1].rstrip("\r\n")
                previous = None
            elif line and line[0] in {" ", "-", "+"}:
                previous = line[0]
                # diff 끝에 줄바꿈이 없다고 소스 EOF 표시가 되는 것은 아니다.
                content = line[1:] if line.endswith("\n") else line[1:] + "\n"
                if previous in {" ", "-"}:
                    old.append(content)
                if previous in {" ", "+"}:
                    new.append(content)
                changed |= previous in {"-", "+"}
            else:
                raise ValueError("patch_invalid_hunk")
            index += 1
        if not old or not changed:
            raise ValueError("patch_invalid_hunk")
        result[path].append((old, new))
    if not result or any(not items for items in result.values()):
        raise ValueError("patch_missing_hunk")
    return result


def _apply_context(source: str, hunks: list[tuple[list[str], list[str]]]) -> str:
    lines = source.splitlines(keepends=True)
    keys = [line.rstrip("\r\n") for line in lines]
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    edits = []
    for old, new in hunks:
        context = [line.rstrip("\r\n") for line in old]
        matches = [
            start
            for start in range(len(lines) - len(old) + 1)
            if keys[start : start + len(old)] == context
        ]
        if len(matches) != 1:
            raise ValueError("patch_context_ambiguous" if matches else "patch_context_not_found")
        start = matches[0]
        replacement = [
            line.rstrip("\r\n") + newline if line.endswith("\n") else line for line in new
        ]
        edits.append((start, start + len(old), replacement))
    edits.sort()
    if any(left[1] > right[0] for left, right in zip(edits, edits[1:], strict=False)):
        raise ValueError("patch_overlapping_hunks")
    for start, end, replacement in reversed(edits):
        lines[start:end] = replacement
    return "".join(lines)


def _project(original: str, visible: str, updated: str) -> str:
    raw = original.splitlines(keepends=True)
    masked = visible.splitlines(keepends=True)
    after = updated.splitlines(keepends=True)
    if len(raw) != len(masked):
        raise ValueError("masked_source_unaligned")
    result = []
    for kind, start, end, new_start, new_end in SequenceMatcher(
        None, masked, after, autojunk=False
    ).get_opcodes():
        if kind == "equal":
            # 바뀌지 않은 줄은 원래 바이트를 유지하고, 표시 값을 거꾸로 되돌리지 않는다.
            result.extend(raw[start:end])
        else:
            replacement = after[new_start:new_end]
            if any(MARKER in line for line in replacement):
                raise ValueError("masked_patch_unresolved")
            result.extend(replacement)
    return "".join(result)


def canonical_patch(
    before: dict[str, str],
    diff: str,
    allowed: set[str],
    masked_sources: dict[str, str],
) -> str:
    candidate = dict(before)
    for path, hunks in _hunks(diff, allowed).items():
        if path not in before:
            raise ValueError("patch_path_not_allowed")
        masked = any(MARKER in line for old, new in hunks for line in old + new)
        if masked:
            if path not in masked_sources:
                raise ValueError("masked_source_unaligned")
            visible = masked_sources[path]
            candidate[path] = _project(before[path], visible, _apply_context(visible, hunks))
        else:
            candidate[path] = _apply_context(before[path], hunks)
    return make_diff(before, candidate)

"""앱 코드를 실행하지 않고, LLM의 첫 안전 수정을 규칙이 고른 AST 노드로 제한한다."""

import ast
import re
import tokenize
from difflib import SequenceMatcher
from io import BytesIO, StringIO

from ai.credentials import DUMMY_SECRET, credential_name
from ai.detectors.rules import OTHER_DB
from ai.models import Violation
from ai.source_names import aliases, qualified


def rebase_targets(
    reference: dict[str, str], current: dict[str, str], targets: list[Violation]
) -> list[Violation]:
    """ID는 유지하되, 템플릿과 앞서 수락된 패치를 적용한 뒤 바뀌지 않은 줄의 위치를 다시 찾는다."""
    mappings: dict[str, dict[int, int]] = {}
    result = []
    for target in targets:
        if target.file not in reference or target.file not in current:
            raise ValueError("patch_target_not_found")
        if target.file not in mappings:
            reference_lines = reference[target.file].splitlines()
            current_lines = current[target.file].splitlines()
            matcher = SequenceMatcher(
                None,
                reference_lines,
                current_lines,
                autojunk=False,
            )
            mappings[target.file] = {
                block.a + offset + 1: block.b + offset + 1
                for block in matcher.get_matching_blocks()
                for offset in range(block.size)
            }
        line = mappings[target.file].get(target.line)
        reference_lines = reference[target.file].splitlines()
        current_lines = current[target.file].splitlines()
        anchor = (
            reference_lines[target.line - 1] if 1 <= target.line <= len(reference_lines) else None
        )
        if line is None or (
            anchor is not None
            and reference_lines.count(anchor) > 1
            and reference_lines.count(anchor) != current_lines.count(anchor)
        ):
            raise ValueError("patch_target_not_found")
        result.append(target.model_copy(update={"line": line}))
    return result


def _same(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def _resolves(node: ast.AST, imports: dict[str, str], expected: str) -> bool:
    root = node
    while isinstance(root, ast.Attribute):
        root = root.value
    return (
        isinstance(root, ast.Name) and root.id in imports and qualified(node, imports) == expected
    )


def _environment(node: ast.AST, imports: dict[str, str], name: str, *, port: bool = False) -> bool:
    if isinstance(node, ast.Subscript):
        return (
            _resolves(node.value, imports, "os.environ")
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == name
        )
    if not port or not isinstance(node, ast.Call):
        return False
    if not any(_resolves(node.func, imports, name) for name in {"os.getenv", "os.environ.get"}):
        return False
    if not 1 <= len(node.args) <= 2 or not isinstance(node.args[0], ast.Constant):
        return False
    if node.args[0].value != name or any(k.arg != "default" for k in node.keywords):
        return False
    defaults = node.args[1:] + [k.value for k in node.keywords]
    return len(defaults) <= 1 and all(
        isinstance(value, ast.Constant)
        and type(value.value) in {str, int}
        and str(value.value) == "8080"
        for value in defaults
    )


def _replacement(
    old: ast.AST, new: ast.AST, rule: str, imports: dict[str, str], env_name: str | None
) -> bool:
    if rule in {"hardcoded_secret", "hardcoded_db_url"}:
        return env_name is not None and _environment(new, imports, env_name)
    if not isinstance(old, ast.Call) or not isinstance(new, ast.Call):
        return False
    if rule == "fixed_port":
        ports = [k.value for k in new.keywords if k.arg == "port"]
        if len(ports) != 1:
            return False
        port = ports[0]
        if not (
            isinstance(port, ast.Call)
            and isinstance(port.func, ast.Name)
            and port.func.id == "int"
            and len(port.args) == 1
            and not port.keywords
            and _environment(port.args[0], imports, "PORT", port=True)
        ):
            return False
        expected = ast.Call(
            func=old.func,
            args=old.args,
            keywords=[
                ast.keyword(arg=k.arg, value=port if k.arg == "port" else k.value)
                for k in old.keywords
            ],
        )
        return _same(expected, new)
    if rule == "file_log" and qualified(old.func, imports) == "logging.basicConfig":
        keywords = [k for k in old.keywords if k.arg not in {"filename", "filemode"}]
        expected = ast.Call(func=old.func, args=old.args, keywords=keywords)
        if _same(expected, new):
            return True
        if any(k.arg in {"stream", "handlers"} for k in keywords):
            return False
        streams = [k.value for k in new.keywords if k.arg == "stream"]
        without_stream = ast.Call(
            func=new.func,
            args=new.args,
            keywords=[k for k in new.keywords if k.arg != "stream"],
        )
        return (
            len(streams) == 1
            and _resolves(streams[0], imports, "sys.stdout")
            and _same(expected, without_stream)
        )
    if rule == "file_log":
        return (
            _resolves(new.func, imports, "logging.StreamHandler")
            and len(new.args) == 1
            and not new.keywords
            and _resolves(new.args[0], imports, "sys.stdout")
        )
    return False


def _anchors(tree: ast.Module, targets: list[Violation]) -> dict[int, tuple[str, str | None]]:
    imports = aliases(tree)
    anchors = {}
    for target in targets:
        found = False
        for node in ast.walk(tree):
            if getattr(node, "lineno", None) != target.line:
                continue
            name = qualified(node.func, imports) if isinstance(node, ast.Call) else ""
            env_name = None
            matches = False
            if target.rule == "file_log":
                matches = name in {
                    "logging.FileHandler",
                    "logging.handlers.RotatingFileHandler",
                    "logging.handlers.TimedRotatingFileHandler",
                } or (
                    name == "logging.basicConfig"
                    and any(k.arg == "filename" for k in node.keywords)
                )
            elif target.rule == "fixed_port":
                matches = name in {"uvicorn.run", "uvicorn.Config"} and any(
                    k.arg == "port"
                    and isinstance(k.value, ast.Constant)
                    and type(k.value.value) is int
                    for k in node.keywords
                )
            elif target.rule == "hardcoded_db_url" and isinstance(node, ast.Constant):
                matches = (
                    isinstance(node.value, str)
                    and (match := OTHER_DB.search(node.value)) is not None
                    and match.end() < len(node.value)
                )
                env_name = "DATABASE_URL"
            elif target.rule == "hardcoded_secret" and isinstance(
                node, (ast.Assign, ast.AnnAssign)
            ):
                names = node.targets if isinstance(node, ast.Assign) else [node.target]
                if (
                    len(names) == 1
                    and isinstance(names[0], ast.Name)
                    and credential_name(names[0].id)
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == DUMMY_SECRET
                ):
                    node, env_name, matches = node.value, names[0].id.upper(), True
            if matches:
                anchors[id(node)] = (target.rule, env_name)
                found = True
        if not found:
            raise ValueError("patch_target_not_found")
    return anchors


def _strip_added_imports(before: ast.Module, after: ast.Module) -> None:
    if (
        before.body
        and isinstance(before.body[0], ast.Expr)
        and isinstance(before.body[0].value, ast.Constant)
        and isinstance(before.body[0].value.value, str)
        and (not after.body or not _same(before.body[0], after.body[0]))
    ):
        raise ValueError("patch_scope_violation")
    existing = {ast.dump(n) for n in before.body if isinstance(n, (ast.Import, ast.ImportFrom))}
    assigned = (
        {n.id for n in ast.walk(before) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        | {
            n.name
            for n in ast.walk(before)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        | {n.arg for n in ast.walk(before) if isinstance(n, ast.arg)}
    )
    previous_imports = aliases(before)
    kept = []
    import_section = True
    for index, node in enumerate(after.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if ast.dump(node) not in existing:
                if not (
                    import_section
                    and isinstance(node, ast.Import)
                    and all(
                        a.name in {"os", "sys", "logging"} and a.asname is None for a in node.names
                    )
                ):
                    raise ValueError("patch_scope_violation")
                if any(
                    a.name in assigned or previous_imports.get(a.name, a.name) != a.name
                    for a in node.names
                ):
                    raise ValueError("patch_scope_violation")
                continue
        elif not (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            import_section = False
        kept.append(node)
    after.body = kept


def _equivalent(
    old: object,
    new: object,
    anchors: dict[int, tuple[str, str | None]],
    imports: dict[str, str],
    spans: list[tuple[ast.AST, ast.AST]],
) -> bool:
    if isinstance(old, ast.AST) and isinstance(new, ast.AST):
        if id(old) in anchors:
            spans.append((old, new))
            rule, env_name = anchors[id(old)]
            return _same(old, new) or _replacement(old, new, rule, imports, env_name)
        return type(old) is type(new) and all(
            _equivalent(value, getattr(new, field), anchors, imports, spans)
            for field, value in ast.iter_fields(old)
        )
    if isinstance(old, list) and isinstance(new, list):
        return len(old) == len(new) and all(
            _equivalent(a, b, anchors, imports, spans) for a, b in zip(old, new, strict=True)
        )
    return type(old) is type(new) and old == new


def _encoding(source: str) -> tuple[str, tuple[str, ...]]:
    """실제로 적용되는 인코딩과 명시적 인코딩 선언(새 UTF-8 선언 포함)을 보호한다."""
    try:
        encoding, _ = tokenize.detect_encoding(BytesIO(source.encode("utf-8")).readline)
    except (SyntaxError, LookupError):
        raise ValueError("patch_scope_violation") from None
    cookies = tuple(
        match.group(1)
        for line in source.splitlines()[:2]
        if (match := re.match(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)", line))
    )
    return encoding, cookies


def _outside_comments(source: str, nodes: list[ast.AST]) -> list[str]:
    """대상 범위 안의 수정과 줄 번호 이동은 허용하면서 주석 내용과 순서를 비교한다."""
    ranges = [(node.lineno, node.end_lineno) for node in nodes]
    try:
        return [
            token.string
            for token in tokenize.generate_tokens(StringIO(source).readline)
            if token.type == tokenize.COMMENT
            and not any(start <= token.start[0] <= end for start, end in ranges)
        ]
    except (tokenize.TokenError, IndentationError):
        raise ValueError("patch_scope_violation") from None


def check_patch_scope(
    before: dict[str, str], after: dict[str, str], paths: set[str], targets: list[Violation]
) -> None:
    for path in paths:
        if not path.endswith(".py") or path not in before:
            raise ValueError("patch_scope_violation")
        if _encoding(before[path]) != _encoding(after[path]):
            raise ValueError("patch_scope_violation")
        old = ast.parse(before[path])
        new = ast.parse(after[path])
        anchors = _anchors(old, [t for t in targets if t.file == path])
        imports = aliases(new)
        _strip_added_imports(old, new)
        spans: list[tuple[ast.AST, ast.AST]] = []
        if not _equivalent(old, new, anchors, imports, spans):
            raise ValueError("patch_scope_violation")
        if _outside_comments(before[path], [a for a, _ in spans]) != _outside_comments(
            after[path], [b for _, b in spans]
        ):
            raise ValueError("patch_scope_violation")

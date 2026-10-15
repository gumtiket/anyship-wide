import ast
import re
import tomllib
from collections.abc import Callable

from packaging.requirements import InvalidRequirement, Requirement

from ai.credentials import credential_name, secret_literals
from ai.detectors.repo import RepoView, aliases, qualified
from ai.models import Violation

RULE_INFO = {
    "sqlite_usage": (6, False, "risky"),
    "hardcoded_secret": (3, True, "safe"),
    "file_log": (11, True, "safe"),
    "fixed_port": (7, True, "safe"),
    "local_file_write": (6, False, "risky"),
    "unpinned_dependency": (2, False, "safe"),
    "hardcoded_db_url": (4, True, "safe"),
    "missing_dependency_declaration": (2, False, "safe"),
}
SQLITE = re.compile(r"^sqlite(?:\+[a-z0-9_]+)?://", re.I)
OTHER_DB = re.compile(r"^(?:postgres(?:ql)?|mysql|mariadb|mssql|oracle)(?:\+[a-z0-9_]+)?://", re.I)


def violation(rule: str, file: str, line: int, evidence: str, *, review: bool = False) -> Violation:
    factor, auto, change_class = RULE_INFO[rule]
    return Violation(
        id=f"{rule}:{file}:{line}",
        factor=factor,
        rule=rule,
        file=file,
        line=line,
        evidence=evidence,
        auto_fixable=auto,
        change_class=change_class,
        confidence="needs_review" if review else "confirmed",
    )


def _ast_rule(
    repo: RepoView, rule: str, predicate: Callable[[ast.AST, dict[str, str]], str | None]
) -> list[Violation]:
    result = []
    for file, tree in repo.modules():
        imports = aliases(tree)
        for node in ast.walk(tree):
            evidence = predicate(node, imports)
            if evidence:
                result.append(
                    violation(rule, file, node.lineno, evidence, review=rule == "local_file_write")
                )
    return sorted(
        {(v.file, v.line, v.rule): v for v in result}.values(),
        key=lambda v: (v.file, v.line, v.rule),
    )


def detect_sqlite(repo: RepoView) -> list[Violation]:
    found = _url_rule(
        repo, "sqlite_usage", SQLITE, "SQLite DB URL을 코드/설정에서 발견했습니다. 값은 생략합니다."
    )
    direct = _ast_rule(
        repo,
        "sqlite_usage",
        lambda node, imports: (
            "SQLite 직접 연결 호출을 발견했습니다. 파일 DB는 외부 자원 전환 검토가 필요합니다."
            if isinstance(node, ast.Call)
            and qualified(node.func, imports) in {"sqlite3.connect", "aiosqlite.connect"}
            and not (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == ":memory:"
            )
            else None
        ),
    )
    return list({(v.file, v.line): v for v in [*found, *direct]}.values())


def detect_db_url(repo: RepoView) -> list[Violation]:
    # SQLite는 sqlite_usage로 표현해 샘플에서 같은 위반이 두 번 나오지 않게 한다.
    return _url_rule(
        repo,
        "hardcoded_db_url",
        OTHER_DB,
        "DB 연결 URL이 코드/설정의 리터럴입니다. 자격 증명은 출력하지 않습니다.",
    )


def _url_rule(repo: RepoView, rule: str, pattern: re.Pattern, evidence: str) -> list[Violation]:
    found = _ast_rule(
        repo,
        rule,
        lambda node, _: (
            evidence
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (match := pattern.search(node.value))
            and match.end() < len(node.value)
            else None
        ),
    )
    for file in repo.files():
        if file.endswith((".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf")):
            for number, text in enumerate(repo.read(file).splitlines(), 1):
                if text.lstrip().startswith("#"):
                    continue
                if any(
                    (match := pattern.search(value := token.strip("'\"")))
                    and match.end() < len(value)
                    for token in re.split(r"[\s=]+", text)
                ):
                    found.append(violation(rule, file, number, evidence))
    return found


def detect_secrets(repo: RepoView) -> list[Violation]:
    found = []
    for file, tree in repo.modules():
        assignments = {
            n.value
            for n in ast.walk(tree)
            if isinstance(n, (ast.Assign, ast.AnnAssign))
            and isinstance(n.value, ast.Constant)
            and any(
                credential_name(getattr(t, "id", getattr(t, "attr", "")))
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
            )
        }
        for node in secret_literals(tree):
            found.append(
                violation(
                    "hardcoded_secret",
                    file,
                    node.lineno,
                    "인증 정보 이름의 변수에 리터럴을 대입합니다. 값은 [REDACTED]입니다."
                    if node in assignments
                    else "자격 증명 리터럴을 발견했습니다. 값은 [REDACTED]입니다.",
                )
            )
    return list({(v.file, v.line): v for v in found}.values())


def detect_file_log(repo: RepoView) -> list[Violation]:
    def check(node: ast.AST, imports: dict[str, str]) -> str | None:
        if isinstance(node, ast.Call):
            name = qualified(node.func, imports)
            if name in {
                "logging.FileHandler",
                "logging.handlers.RotatingFileHandler",
                "logging.handlers.TimedRotatingFileHandler",
            } or (
                name == "logging.basicConfig" and any(k.arg == "filename" for k in node.keywords)
            ):
                return "파일 로그 핸들러/filename 설정을 발견했습니다."
        return None

    return _ast_rule(repo, "file_log", check)


def detect_fixed_port(repo: RepoView) -> list[Violation]:
    def check(node: ast.AST, imports: dict[str, str]) -> str | None:
        if isinstance(node, ast.Call) and qualified(node.func, imports) in {
            "uvicorn.run",
            "uvicorn.Config",
        }:
            if any(
                k.arg == "port"
                and isinstance(k.value, ast.Constant)
                and isinstance(k.value.value, int)
                for k in node.keywords
            ):
                return "uvicorn 시작 포트를 정수 리터럴로 지정합니다."
        return None

    return _ast_rule(repo, "fixed_port", check)


def detect_local_write(repo: RepoView) -> list[Violation]:
    def check(node: ast.AST, imports: dict[str, str]) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        name = qualified(node.func, imports)
        if name in {"open", "io.open"}:
            mode = (
                node.args[1]
                if len(node.args) > 1
                else next((k.value for k in node.keywords if k.arg == "mode"), None)
            )
            if (
                isinstance(mode, ast.Constant)
                and isinstance(mode.value, str)
                and set(mode.value) & set("wax+")
            ):
                return (
                    "파일 쓰기 모드를 발견했습니다. 영속성/외부 저장 여부는 추가 검토가 필요합니다."
                )
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"write_text", "write_bytes"}:
            return "파일 쓰기 메서드 호출. 영속성/저장 위치는 추가 검토가 필요합니다."
        return None

    return _ast_rule(repo, "local_file_write", check)


def _unpinned(text: str) -> bool:
    try:
        req = Requirement(text)
    except InvalidRequirement:
        return True
    return req.url is not None or not any(
        s.operator in {"==", "==="} and "*" not in s.version for s in req.specifier
    )


def detect_dependencies(repo: RepoView) -> list[Violation]:
    result = []
    if not any(
        re.search(
            r"(?:^|/)(?:requirements[^/]*\.txt|pyproject\.toml|Pipfile|setup\.py|setup\.cfg)$", file
        )
        and any(
            line.strip() and not line.lstrip().startswith("#")
            for line in repo.read(file).splitlines()
        )
        for file in repo.files()
    ):
        python = next((file for file in repo.files() if file.endswith(".py")), None)
        if python:
            result.append(
                violation(
                    "missing_dependency_declaration",
                    python,
                    1,
                    "의존성 선언 파일이 없거나 비어 있습니다. 배포 전 의존성 선언이 필요합니다.",
                )
            )
    for file in repo.files():
        entries = []
        if re.search(r"(?:^|/)requirements[^/]*\.txt$", file):
            entries = [
                (line, text.split(" #", 1)[0].strip())
                for line, text in enumerate(repo.read(file).splitlines(), 1)
                if text.strip() and not text.lstrip().startswith(("#", "-"))
            ]
        elif file.endswith("pyproject.toml"):
            try:
                data = tomllib.loads(repo.read(file))
                deps = data.get("project", {}).get("dependencies", [])
                entries = [
                    (
                        next(
                            (
                                i
                                for i, line in enumerate(repo.read(file).splitlines(), 1)
                                if f'"{dep}"' in line or f"'{dep}'" in line
                            ),
                            1,
                        ),
                        dep,
                    )
                    for dep in deps
                ]
            except tomllib.TOMLDecodeError:
                continue
        misses = [(line, text) for line, text in entries if _unpinned(text)]
        if misses:
            result.append(
                violation(
                    "unpinned_dependency",
                    file,
                    misses[0][0],
                    f"이 선언 파일에서 {len(misses)}개 의존성의 정확한 버전 고정이 미확인입니다.",
                )
            )
    return result


DETECTORS = (
    detect_sqlite,
    detect_secrets,
    detect_file_log,
    detect_fixed_port,
    detect_local_write,
    detect_dependencies,
    detect_db_url,
)


def detect(repo: RepoView) -> list[Violation]:
    return sorted(
        (v for detector in DETECTORS for v in detector(repo)),
        key=lambda v: (v.file, v.line, v.rule),
    )

import ast

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from ai.detectors.repo import aliases, qualified
from ai.models import Diagnosis, EnvVar, TransformReport, WarningItem
from ai.security import DUMMY_SECRET, SourceMasker, credential_name, edit_nodes
from ai.spec.env_policy import allowed_name
from ai.transform.dockerfile import harden_dockerignore


def has_psycopg2(requirements: str) -> bool:
    """조건 없는 psycopg2 배포판만 선택된 드라이버를 제공한 것으로 본다."""
    for line in requirements.splitlines():
        try:
            dependency = Requirement(line.split(" #", 1)[0].strip())
        except InvalidRequirement:
            continue
        if (
            canonicalize_name(dependency.name) in {"psycopg2", "psycopg2-binary"}
            and dependency.marker is None
            and dependency.url is None
        ):
            return True
    return False


def add_imports(source: str, modules: set[str]) -> str:
    tree = ast.parse(source)
    existing = {
        item.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for item in node.names
        if not item.asname or item.asname == item.name
    }
    missing = sorted(modules - existing)
    if not missing:
        return source
    insertion_line = 0
    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ) or (isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            insertion_line = node.end_lineno
        else:
            break
    lines = source.splitlines(keepends=True)
    lines[insertion_line:insertion_line] = [f"import {name}\n" for name in missing]
    return "".join(lines)


def template_changes(
    before: dict[str, str],
    diagnosis: Diagnosis,
    masker: SourceMasker,
    sample_name: str | None,
) -> tuple[dict[str, str], TransformReport]:
    after = dict(before)
    if ".dockerignore" not in masker.blocked_files:
        existing = before.get(".dockerignore", "")
        # 기존 무시 패턴은 유지하되, 필수 제외 항목을 맨 뒤(모든 ! 뒤)에 덧붙인다.
        after[".dockerignore"] = harden_dockerignore(existing)
    environment = {}
    report = TransformReport(sample_name=sample_name)
    addressed = set()
    database_changed = False
    for file in sorted(before):
        if file in masker.blocked_files:
            if any(v.file == file for v in diagnosis.violations):
                report.warnings.append(
                    WarningItem(
                        code="secret_file_deferred",
                        message=f"{file}: 민감 값의 diff 노출 방지를 위해 수정을 보류했습니다.",
                    )
                )
            continue
        if not file.endswith(".py"):
            continue
        source = before[file]
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        imports = aliases(tree)
        edits = []
        required_imports = set()
        for node in ast.walk(tree):
            rule = None
            replacement = None
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if (
                    len(targets) == 1
                    and isinstance(targets[0], ast.Name)
                    and credential_name(targets[0].id)
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == DUMMY_SECRET
                ):
                    key = targets[0].id.upper()
                    if not allowed_name(key):
                        report.warnings.append(
                            WarningItem(
                                code="environment_name_forbidden",
                                message=f"{key}: C 예약 이름으로 추출할 수 없어 보류했습니다.",
                            )
                        )
                        continue
                    edits.append((node.value, f'os.environ["{key}"]'))
                    required_imports.add("os")
                    environment[key] = EnvVar(
                        name=key, secret=True, required=True, generate=key == "SECRET_KEY"
                    )
                    rule = "hardcoded_secret"
            if isinstance(node, ast.Call):
                name = qualified(node.func, imports)
                if (
                    name == "sqlalchemy.create_engine"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value.startswith("sqlite")
                ):
                    copied = ast.parse(ast.unparse(node), mode="eval").body
                    copied.args[0] = ast.parse(
                        'os.environ["DATABASE_URL"].replace('
                        '"postgresql://", "postgresql+psycopg2://", 1)',
                        mode="eval",
                    ).body
                    for keyword in copied.keywords:
                        if keyword.arg == "connect_args" and isinstance(keyword.value, ast.Dict):
                            pairs = [
                                (key, value)
                                for key, value in zip(
                                    keyword.value.keys, keyword.value.values, strict=True
                                )
                                if not isinstance(key, ast.Constant)
                                or key.value != "check_same_thread"
                            ]
                            keyword.value.keys = [key for key, _ in pairs]
                            keyword.value.values = [value for _, value in pairs]
                    replacement, rule = ast.unparse(copied), "sqlite_usage"
                    database_changed = True
                    required_imports.add("os")
                    environment["DATABASE_URL"] = EnvVar(
                        name="DATABASE_URL", secret=True, required=True
                    )
                elif name in {
                    "logging.FileHandler",
                    "logging.handlers.RotatingFileHandler",
                    "logging.handlers.TimedRotatingFileHandler",
                }:
                    replacement, rule = "logging.StreamHandler(sys.stdout)", "file_log"
                    required_imports.update({"logging", "sys"})
                elif name in {"uvicorn.run", "uvicorn.Config"}:
                    for keyword in node.keywords:
                        if (
                            keyword.arg == "port"
                            and isinstance(keyword.value, ast.Constant)
                            and isinstance(keyword.value.value, int)
                        ):
                            edits.append((keyword.value, 'int(os.environ.get("PORT", "8080"))'))
                            required_imports.add("os")
                            environment["PORT"] = EnvVar(name="PORT", default="8080")
                            rule = "fixed_port"
                if sample_name and name in {"initialize", "app.db.initialize"}:
                    replacement = "None  # DB 초기화는 python -m app.migrate로 분리"
                if name.endswith(".setLevel") and any(
                    v.rule == "file_log" and v.file == file for v in diagnosis.violations
                ):
                    replacement = (
                        f'{ast.unparse(node.func)}(os.environ.get("LOG_LEVEL", "INFO").upper())'
                    )
                    required_imports.add("os")
                    environment["LOG_LEVEL"] = EnvVar(name="LOG_LEVEL", default="INFO")
                if replacement:
                    edits.append((node, replacement))
            if rule:
                addressed.update(
                    v.id
                    for v in diagnosis.violations
                    if v.source == "rule"
                    and v.rule == rule
                    and v.file == file
                    and node.lineno <= v.line <= node.end_lineno
                )
        if edits:
            try:
                after[file] = add_imports(edit_nodes(source, edits), required_imports)
                ast.parse(after[file])
            except (SyntaxError, ValueError):
                after[file] = source
                addressed.difference_update(v.id for v in diagnosis.violations if v.file == file)
                report.warnings.append(
                    WarningItem(
                        code="template_failed", message=f"{file}: 템플릿 변환을 보류했습니다."
                    )
                )
    if diagnosis.framework and diagnosis.framework.entrypoint:
        module, variable = diagnosis.framework.entrypoint.split(":")
        file = module.replace(".", "/") + ".py"
        if file in after and file not in masker.blocked_files:
            tree = ast.parse(after[file])
            has_health = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "/healthz"
                for node in ast.walk(tree)
            )
            if not has_health:
                names = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                function = "_bronze_healthz"
                while function in names:
                    function += "_"
                after[file] = (
                    after[file].rstrip()
                    + f'\n\n\n@{variable}.get("/healthz")\n'
                    + f"def {function}() -> dict[str, str]:\n"
                    + '    return {"status": "ok"}\n'
                )
    database_changed = any(
        v.rule == "sqlite_usage" and v.id in addressed for v in diagnosis.violations
    )
    if database_changed:
        report.needs_approval = True
        report.warnings.append(
            WarningItem(
                code="data_migration_unsupported",
                message="기존 데이터는 자동으로 이전되지 않는다. 데이터 이전 필요(미지원)",
            )
        )
        requirements = next((name for name in after if name == "requirements.txt"), None)
        if requirements in masker.blocked_files:
            report.warnings.append(
                WarningItem(
                    code="driver_dependency_deferred",
                    message="선언 파일의 민감 URI 노출 방지를 위해 드라이버 추가를 보류했습니다.",
                )
            )
        elif requirements is not None and not has_psycopg2(after[requirements]):
            after[requirements] = after[requirements].rstrip() + "\npsycopg2-binary\n"
        elif requirements is None:
            report.warnings.append(
                WarningItem(
                    code="driver_dependency_deferred",
                    message="선언 파일이 없어 Postgres 드라이버 추가를 보류했습니다.",
                )
            )
        if sample_name:
            after["app/migrate.py"] = (
                "from app.db import Base, engine\n\n\n"
                "def main() -> None:\n    Base.metadata.create_all(engine)\n\n\n"
                'if __name__ == "__main__":\n    main()\n'
            )
            report.migrate_command = "python -m app.migrate"
    for item in diagnosis.violations:
        if item.rule == "local_file_write":
            report.needs_approval = True
            report.warnings.append(
                WarningItem(
                    code="file_storage_change_deferred",
                    message="영속 파일 저장 전환은 승인 필요이며 /tmp로 자동 이동하지 않았습니다.",
                )
            )
        elif item.rule == "unpinned_dependency":
            report.warnings.append(
                WarningItem(
                    code="dependency_version_unknown",
                    message="레포의 실제 사용 버전을 알 수 없어 버전 핀을 추측하지 않았습니다.",
                )
            )
    report.addressed_ids = sorted(addressed)
    report.deferred_ids = sorted(v.id for v in diagnosis.violations if v.id not in addressed)
    report.env_vars = [environment[key] for key in sorted(environment)]
    report.changed_files = sorted(file for file in after if after[file] != before.get(file))
    report.status = "partial" if report.deferred_ids else "proposed"
    return after, report

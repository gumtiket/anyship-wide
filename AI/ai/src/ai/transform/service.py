import ast
import hashlib
import json
import re
from importlib.resources import files

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pydantic import Field

from ai.credentials import credential_urls
from ai.detectors import RepoView, detect, detect_framework, detect_signals
from ai.detectors.repo import aliases, qualified
from ai.llm.base import LLMClient
from ai.models import Diagnosis, EnvVar, OutputModel, TransformReport, Violation, WarningItem
from ai.security import SourceMasker, credential_name
from ai.spec.env_policy import allowed_name, allowed_value
from ai.transform.patch import canonical_patch
from ai.transform.scope import check_patch_scope, rebase_targets
from ai.transform.templates import template_changes
from ai.transform.workspace import Workspace, make_diff, patch_paths, source_files


class PatchResponse(OutputModel):
    diff: str = Field(max_length=100000)
    violation_ids: list[str] = Field(default_factory=list)


def _failure_code(error: ValueError | RuntimeError | SyntaxError) -> str:
    # 피드백·경고에는 고정된 검증 코드만 넣는다. 소스나 SDK 텍스트는 넣지 않는다.
    code = str(error) if isinstance(error, ValueError) else "llm_patch_failed"
    allowed = {
        "patch_path_not_allowed",
        "patch_path_prefix",
        "patch_metadata_forbidden",
        "patch_delete_or_rename_forbidden",
        "patch_non_diff_content",
        "patch_missing_hunk",
        "git_apply_check_failed",
        "git_apply_failed",
        "compileall_failed",
        "repeated_patch",
        "dependency_change_not_allowed",
        "model_class_change_not_allowed",
        "non_python_change_not_allowed",
        "unsafe_call_added",
        "sensitive_value_in_patch",
        "sensitive_literal_added",
        "invalid_patch_response",
        "unexpected_violation_ids",
        "signals_changed",
        "framework_changed",
        "requested_violation_unresolved",
        "environment_name_forbidden",
        "environment_value_unsafe",
        "environment_value_required",
        "git_timeout",
        "patch_scope_violation",
        "patch_target_not_found",
        "patch_invalid_hunk",
        "patch_duplicate_file",
        "patch_context_not_found",
        "patch_context_ambiguous",
        "patch_overlapping_hunks",
        "masked_patch_unresolved",
        "masked_source_unaligned",
    }
    return code if code in allowed else "llm_patch_failed"


def identify_sample(repo: RepoView) -> str | None:
    manifest = json.loads(files("ai").joinpath("sample-manifests.json").read_text(encoding="utf-8"))
    observed = {
        name: hashlib.sha256(repo.read(name).encode()).hexdigest()
        for name in repo.files()
        if name.endswith((".py", ".html")) or name == "requirements.txt"
    }
    return next((name for name, hashes in manifest.items() if hashes == observed), None)


def _classes(source: str) -> list[str]:
    return [
        ast.dump(node, include_attributes=False)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef)
    ]


def _driver_dependency_allowed(line: str) -> bool:
    try:
        dependency = Requirement(line)
    except InvalidRequirement:
        return False
    return (
        canonicalize_name(dependency.name) == "psycopg2-binary"
        and not dependency.extras
        and dependency.url is None
        and dependency.marker is None
    )


def _check_semantics(before: dict[str, str], after: dict[str, str], paths: set[str]) -> None:
    forbidden = {
        "eval",
        "exec",
        "__import__",
        "os.system",
        "os.popen",
        "subprocess.run",
        "subprocess.Popen",
        "pickle.loads",
        "Path.write_text",
        "Path.write_bytes",
    }
    for path in paths:
        if not path.endswith(".py"):
            if path.endswith("requirements.txt"):
                old = {line.strip() for line in before.get(path, "").splitlines() if line.strip()}
                new = {line.strip() for line in after[path].splitlines() if line.strip()}
                if old - new or any(not _driver_dependency_allowed(line) for line in new - old):
                    raise ValueError("dependency_change_not_allowed")
            else:
                raise ValueError("non_python_change_not_allowed")
            continue
        if _classes(before.get(path, "")) != _classes(after[path]):
            raise ValueError("model_class_change_not_allowed")
        old_calls = {
            ast.unparse(n.func)
            for n in ast.walk(ast.parse(before.get(path, "")))
            if isinstance(n, ast.Call)
        }
        new_calls = {
            ast.unparse(n.func) for n in ast.walk(ast.parse(after[path])) if isinstance(n, ast.Call)
        }
        safe_calls = {
            "int",
            "str",
            "os.getenv",
            "os.environ.get",
            "logging.StreamHandler",
            "logging.basicConfig",
            "logging.getLogger",
        }
        framework = detect_framework_from_sources(before)
        if framework:
            safe_calls.add(framework.split(":")[-1] + ".get")
        if any(
            name in forbidden
            or name not in safe_calls
            and not (name.startswith("os.") and name.endswith(".upper"))
            for name in new_calls - old_calls
        ):
            raise ValueError("unsafe_call_added")


def detect_framework_from_sources(source: dict[str, str]) -> str | None:
    with Workspace(source) as workspace:
        return detect_framework(RepoView(workspace.root)).entrypoint


def env_vars(candidate: dict[str, str], masker: SourceMasker | None = None) -> list[EnvVar]:
    found = {}
    for file, source in candidate.items():
        if not file.endswith(".py"):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        imports = aliases(tree)
        for node in ast.walk(tree):
            name = None
            required = False
            default = None
            if (
                isinstance(node, ast.Subscript)
                and qualified(node.value, imports) == "os.environ"
                and isinstance(node.slice, ast.Constant)
            ):
                name, required = node.slice.value, True
            elif (
                isinstance(node, ast.Call)
                and qualified(node.func, imports) in {"os.getenv", "os.environ.get"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                name = node.args[0].value
                node_default = (
                    node.args[1]
                    if len(node.args) > 1
                    else next((k.value for k in node.keywords if k.arg == "default"), None)
                )
                if isinstance(node_default, ast.Constant):
                    default = str(node_default.value) if node_default.value is not None else None
            if isinstance(name, str) and name.isidentifier() and name.upper() == name:
                secret = (
                    credential_name(name)
                    or name in {"DATABASE_URL", "REDIS_URL", "STORAGE_URL"}
                    or bool(default and credential_urls(default))
                    or bool(default and re.match(r"(?i)^Bearer\s+\S+", default))
                    or bool(default and masker and masker.contains_sensitive(default))
                )
                item = EnvVar(
                    name=name, secret=secret, required=required, default=None if secret else default
                )
                previous = found.get(name)
                if previous is None:
                    found[name] = item
                else:
                    previous.secret |= item.secret
                    previous.required |= item.required
                    if previous.secret or previous.required or previous.default != item.default:
                        previous.default = None
    return [found[name] for name in sorted(found)]


def _patch_candidate(
    before: dict[str, str],
    diff: str,
    allowed: set[str],
    masker: SourceMasker,
    *,
    targets: list[Violation] | None = None,
    masked_sources: dict[str, str] | None = None,
    system_env: set[str] | None = None,
) -> dict[str, str]:
    if masker.contains_sensitive(diff):
        raise ValueError("sensitive_value_in_patch")
    if targets is not None:
        diff = canonical_patch(before, diff, allowed, masked_sources or {})
    paths = patch_paths(diff, allowed)
    if masker.contains_sensitive(diff):
        raise ValueError("sensitive_value_in_patch")
    with Workspace(before) as workspace:
        before_view = RepoView(workspace.root)
        before_signals = {(s.name, s.file) for s in detect_signals(before_view)}
        before_framework = detect_framework(before_view)
        workspace.apply(diff)
        candidate = workspace.read(set(before) | paths)
        _check_semantics(before, candidate, paths)
        original_env = {e.name: e for e in env_vars(before, masker)}
        for variable in env_vars(candidate, masker):
            if original_env.get(variable.name) == variable:
                continue
            if variable.name not in (system_env or set()) and not allowed_name(variable.name):
                raise ValueError("environment_name_forbidden")
            if variable.default is not None and not allowed_value(variable.default):
                raise ValueError("environment_value_unsafe")
            if (
                variable.name not in (system_env or set())
                and not variable.secret
                and variable.default is None
            ):
                raise ValueError("environment_value_required")
        view = RepoView(workspace.root)
        if SourceMasker(view).blocked_files - masker.blocked_files:
            raise ValueError("sensitive_literal_added")
        if {(s.name, s.file) for s in detect_signals(view)} != before_signals:
            raise ValueError("signals_changed")
        framework = detect_framework(view)
        if (framework.support_grade, framework.entrypoint) != (
            before_framework.support_grade,
            before_framework.entrypoint,
        ):
            raise ValueError("framework_changed")
        if not workspace.compile():
            raise ValueError("compileall_failed")
        if targets is not None:
            check_patch_scope(before, candidate, paths, targets)
    return candidate


def llm_patch(
    before: dict[str, str],
    targets: list[Violation],
    llm: LLMClient | None,
    masker: SourceMasker,
    *,
    reference_sources: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str], int, list[WarningItem]]:
    """첫 시도와 재생성 2회, 그 뒤 대상마다 1회씩 시도한다. 앱은 실행하지 않는다."""
    eligible = [
        v
        for v in targets
        if v.source == "rule"
        and v.auto_fixable
        and v.change_class == "safe"
        and v.confidence == "confirmed"
        and v.file not in masker.blocked_files
        and v.file.endswith(".py")
    ]
    blocked = set()
    for file in {v.file for v in eligible}:
        tree = ast.parse(before[file])
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            names = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(n, ast.Name) and not allowed_name(n.id.upper()) for n in names):
                blocked.update(
                    v.id
                    for v in eligible
                    if v.file == file
                    and node.lineno <= v.line <= node.end_lineno
                    and v.rule in {"hardcoded_secret", "hardcoded_db_url"}
                )
    eligible = [v for v in eligible if v.id not in blocked]
    policy_warnings = [
        WarningItem(
            code="environment_name_forbidden",
            message=f"{identifier}: C 예약 이름의 환경변수 추출을 보류했습니다.",
        )
        for identifier in sorted(blocked)
    ]
    if not eligible or llm is None:
        return before, [], 0, policy_warnings
    prompt = files("ai").joinpath("prompts/transform.txt").read_text(encoding="utf-8")
    attempts = 0
    error_code = None
    seen = set()
    warnings = policy_warnings

    def attempt(
        current: dict[str, str], selected: list[Violation], retry: str | None
    ) -> tuple[dict[str, str], list[str]]:
        nonlocal attempts
        scoped_targets = rebase_targets(reference_sources or before, current, selected)
        with Workspace(current) as workspace:
            view = RepoView(workspace.root)
            masked = SourceMasker(view)
            sources = {}
            masked_sources = {}
            budget = 20000
            for name in sorted({v.file for v in selected}):
                if budget <= 0:
                    break
                masked_sources[name] = masked.source(view, name)
                sources[name] = masked_sources[name][:budget]
                budget -= len(sources[name])
            payload = {
                "source": sources,
                "violations": [v.model_dump(mode="json") for v in scoped_targets],
                "validation_error": retry,
            }
        attempts += 1
        response = llm.complete(
            prompt,
            json.dumps(payload, ensure_ascii=False),
            tier="strong",
            schema=PatchResponse,
            stage="transform",
        )
        proposal = response.parsed
        if not isinstance(proposal, PatchResponse):
            raise ValueError("invalid_patch_response")
        if not set(proposal.violation_ids) <= {v.id for v in selected}:
            raise ValueError("unexpected_violation_ids")
        digest = hashlib.sha256(proposal.diff.encode()).hexdigest()
        if digest in seen:
            raise ValueError("repeated_patch")
        seen.add(digest)
        if not proposal.diff:
            return current, []
        candidate = _patch_candidate(
            current,
            proposal.diff,
            set(sources),
            masker,
            targets=scoped_targets,
            masked_sources=masked_sources,
            system_env={"PORT"} if any(v.rule == "fixed_port" for v in selected) else set(),
        )
        with Workspace(candidate) as workspace:
            remaining = {(v.rule, v.file) for v in detect(RepoView(workspace.root))}
        addressed = [
            v.id
            for v in selected
            if v.id in proposal.violation_ids and (v.rule, v.file) not in remaining
        ]
        if set(proposal.violation_ids) != set(addressed):
            raise ValueError("requested_violation_unresolved")
        return candidate, addressed

    for _ in range(3):
        try:
            candidate, addressed = attempt(before, eligible, error_code)
            return candidate, addressed, attempts, warnings
        except (ValueError, RuntimeError, SyntaxError) as error:
            failure = _failure_code(error)
            if failure.startswith("environment_"):
                warning = WarningItem(
                    code=failure,
                    message="C의 환경변수 이름/값 규칙에 맞지 않는 변경안을 보류했습니다.",
                )
                if warning not in warnings:
                    warnings.append(warning)
            if failure == "repeated_patch":
                break
            error_code = failure
    current, addressed = before, []
    for target in eligible:
        try:
            current, ids = attempt(current, [target], error_code)
            addressed.extend(ids)
        except (ValueError, RuntimeError, SyntaxError) as error:
            reason = _failure_code(error)
            if reason == "repeated_patch":
                reason = error_code or reason
            warnings.append(
                WarningItem(
                    code="llm_transform_deferred",
                    message=(
                        f"{target.id}: [{reason}] 재생성/개별 재시도에 실패해 변경을 보류했습니다."
                    ),
                )
            )
    return current, sorted(set(addressed)), attempts, warnings


def propose(
    repo: RepoView, diagnosis: Diagnosis, llm: LLMClient | None = None
) -> tuple[str, TransformReport]:
    if diagnosis.support_grade != "supported":
        return "", TransformReport(
            warnings=[
                WarningItem(
                    code="transform_unsupported",
                    message="지원이 확정되지 않은 앱은 변환하지 않습니다.",
                )
            ]
        )
    masker = SourceMasker(repo)
    original = source_files(repo)
    sample_name = identify_sample(repo)
    candidate, report = template_changes(original, diagnosis, masker, sample_name)
    pending = [v for v in diagnosis.violations if v.id in report.deferred_ids]
    candidate, addressed, attempts, warnings = llm_patch(
        candidate, pending, llm, masker, reference_sources=original
    )
    report.llm_attempts = attempts
    report.addressed_ids = sorted(set(report.addressed_ids) | set(addressed))
    report.deferred_ids = sorted(
        v.id for v in diagnosis.violations if v.id not in report.addressed_ids
    )
    report.warnings.extend(warnings)
    diff = make_diff(original, candidate)
    report.changed_files = sorted(
        name for name in candidate if candidate[name] != original.get(name)
    )
    generated = {e.name for e in report.env_vars if e.generate}
    report.env_vars = env_vars(candidate, masker)
    for variable in report.env_vars:
        variable.generate = variable.name in generated
    if masker.contains_sensitive(diff):
        report.status = "failed"
        report.changed_files = []
        report.addressed_ids = []
        report.deferred_ids = sorted(v.id for v in diagnosis.violations)
        report.migrate_command = None
        report.env_vars = env_vars(original, masker)
        report.warnings.append(
            WarningItem(
                code="sensitive_diff_blocked",
                message="민감 값이 포함된 diff를 출력하지 않았습니다.",
            )
        )
        return "", report
    try:
        patch_paths(diff, set(candidate))
        with Workspace(original) as workspace:
            workspace.apply(diff)
            report.patch_valid = True
            report.compile_passed = workspace.compile()
            if not report.compile_passed:
                raise ValueError("compileall_failed")
            transformed_signals = {
                (s.name, s.file) for s in detect_signals(RepoView(workspace.root))
            }
            if transformed_signals != {(s.name, s.file) for s in diagnosis.signals}:
                raise ValueError("signals_changed")
    except (ValueError, OSError, TimeoutError):
        report.status = "failed"
        report.patch_valid = False
        report.compile_passed = False
        report.changed_files = []
        report.addressed_ids = []
        report.deferred_ids = sorted(v.id for v in diagnosis.violations)
        report.migrate_command = None
        report.env_vars = env_vars(original)
        report.warnings.append(
            WarningItem(
                code="transform_validation_failed",
                message="최종 변경안의 적용/컴파일/신호 검증에 실패해 diff를 비웠습니다.",
            )
        )
        return "", report
    report.status = "partial" if report.deferred_ids else "proposed"
    return diff, report

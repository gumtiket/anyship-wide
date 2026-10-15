import ast
import hashlib
import json
import time
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import Field, model_validator

from ai.build_context import BuildContext
from ai.detectors import RepoView, detect
from ai.gate.models import GateAttempt
from ai.gate.runner import ContainerRunner
from ai.gate.service import run_gate, safe_log
from ai.llm.base import LLMClient
from ai.models import GateReport, OutputModel
from ai.output_session import register_context
from ai.security import SourceMasker
from ai.spec.models import DeploySpec
from ai.stages import LogFn, Stage
from ai.transform.dockerfile import lint_dockerfile
from ai.transform.service import _patch_candidate, env_vars
from ai.transform.workspace import source_files

MAX_GATE_ATTEMPTS = 3  # 첫 실행과, 수정한 뒤의 재실행 최대 2회.
REPAIRABLE = {"build", "migrate", "app_start", "healthcheck", "postgres_crud"}


class FailureAnalysis(OutputModel):
    summary: str = Field(min_length=1, max_length=1200)
    diff: str = Field(default="", max_length=100000)
    dockerfile: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def proposed_change(self):
        if not self.diff.strip() and not self.dockerfile:
            raise ValueError("repair_change_required")
        return self


def _routes(sources: dict[str, str]) -> dict:
    protected = {}
    for name, text in sources.items():
        if not name.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
                protected[(name, node.name)] = ast.dump(node, include_attributes=False)
    return protected


def repair_context(context: BuildContext, proposal: FailureAnalysis) -> BuildContext:
    if not context.verify_sample():
        raise ValueError("untrusted_repair_context")
    view = RepoView(context.root)
    before = source_files(view)
    masker = SourceMasker(view)
    allowed = {name for name in before if name.endswith(".py")}
    candidate = (
        _patch_candidate(before, proposal.diff, allowed, masker)
        if proposal.diff.strip()
        else dict(before)
    )
    if _routes(candidate) != _routes(before):
        raise ValueError("public_api_change_forbidden")
    if env_vars(candidate) != env_vars(before):
        raise ValueError("environment_contract_change_forbidden")
    if proposal.dockerfile:
        if lint_dockerfile(proposal.dockerfile):
            raise ValueError("dockerfile_repair_lint_failed")
        old_command = next(
            line for line in before["Dockerfile"].splitlines() if line.startswith("CMD ")
        )
        if old_command not in proposal.dockerfile.splitlines():
            raise ValueError("dockerfile_entrypoint_changed")
        candidate["Dockerfile"] = proposal.dockerfile
    if candidate == before:
        raise ValueError("repair_no_change")
    temporary = TemporaryDirectory(prefix="bronze-repair-")
    result = BuildContext(root=temporary.name)
    result._temporary = temporary
    try:
        for name, text in candidate.items():
            destination = Path(result.root) / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
        after_view = RepoView(result.root)
        before_hits = {(v.rule, v.file) for v in detect(view)}
        if {(v.rule, v.file) for v in detect(after_view)} - before_hits:
            raise ValueError("repair_added_rule_violation")
        if not (Path(result.root) / "app/migrate.py").exists():
            raise ValueError("migration_removed")
        result._sample_name = context._sample_name
        result._sealed_digest = result.tree_digest()
    except BaseException:
        result.cleanup()
        raise
    register_context(result)
    return result


def run_with_retry(
    context: BuildContext,
    spec: DeploySpec,
    runner: ContainerRunner,
    log: LogFn,
    llm: LLMClient | None,
    *,
    original_context: BuildContext | None = None,
    timeout_s: int = 40,
) -> tuple[GateReport, BuildContext]:
    current = context
    attempts = []
    proposals = set()
    states = {context.tree_digest()}
    report = GateReport(status="skipped")
    for number in range(1, MAX_GATE_ATTEMPTS + 1):
        log(Stage.VALIDATING, f"게이트 시도 {number}/{MAX_GATE_ATTEMPTS}")
        report = run_gate(
            current,
            spec,
            runner,
            log,
            original_ctx=original_context,
            timeout_s=timeout_s,
            sleep=(lambda _: None) if not runner.real else time.sleep,
        )
        attempt = GateAttempt(
            number=number,
            status=report.status,
            reason=report.reason,
            transformed=report.transformed.model_copy(deep=True),
            original=report.original.model_copy(deep=True),
        )
        attempts.append(attempt)
        report.attempts = attempts
        if report.status != "failed":
            break
        if number == MAX_GATE_ATTEMPTS:
            report.retry_stop_reason = "attempt_limit"
            break
        failed_steps = [s.name for s in report.transformed.steps if s.status == "failed"]
        if report.transformed.cleanup_errors or not set(failed_steps) & REPAIRABLE:
            report.retry_stop_reason = "non_code_failure"
            break
        if llm is None:
            report.retry_stop_reason = "repair_llm_not_requested"
            break
        view = RepoView(current.root)
        masker = SourceMasker(view)
        payload = {
            "failure": safe_log(
                masker.text(
                    json.dumps(report.transformed.model_dump(mode="json"), ensure_ascii=False)
                ),
                [],
            ),
            "reason": safe_log(masker.text(report.reason), []),
            "source": masker.summaries(view),
            "dockerfile": view.read("Dockerfile"),
            "allowed_paths": sorted(name for name in view.files() if name.endswith(".py")),
        }
        try:
            response = llm.complete(
                files("ai").joinpath("prompts/repair.txt").read_text(),
                json.dumps(payload, ensure_ascii=False),
                tier="strong",
                schema=FailureAnalysis,
                stage="repair",
            )
            proposal = response.parsed
            if not isinstance(proposal, FailureAnalysis):
                raise ValueError("repair_response_invalid")
            attempt.repair_summary = safe_log(masker.text(proposal.summary), [])
            digest = hashlib.sha256(
                json.dumps(
                    {"diff": proposal.diff, "dockerfile": proposal.dockerfile}, sort_keys=True
                ).encode()
            ).hexdigest()
            if digest in proposals:
                report.retry_stop_reason = "repeated_patch"
                attempt.repair_status = "rejected"
                break
            proposals.add(digest)
            updated = repair_context(current, proposal)
            if updated.tree_digest() in states:
                updated.cleanup()
                report.retry_stop_reason = "repeated_context"
                attempt.repair_status = "rejected"
                break
            states.add(updated.tree_digest())
            if current is not context:
                current.cleanup()
            current = updated
            attempt.repair_status = "validated"
            log(
                Stage.VALIDATING,
                f"재시도 {number + 1}/{MAX_GATE_ATTEMPTS}: {attempt.repair_summary}",
            )
        except (ValueError, RuntimeError, OSError, SyntaxError):
            report.retry_stop_reason = "repair_rejected_or_unavailable"
            attempt.repair_status = "rejected"
            break
    if report.status == "failed":
        log(
            Stage.FAILED, f"게이트 종료: {report.retry_stop_reason}; 원인·마지막 로그를 저장합니다."
        )
    return report, current

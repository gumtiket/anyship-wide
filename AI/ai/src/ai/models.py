from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ai.build_context import BuildContext
from ai.gate.models import GateAttempt, GateExecution
from ai.spec.models import DeploySpec

# TODO: A/C와 계약 확정 시 최소 필드를 조정한다. P2는 진단과 변경안을 생성한다.


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class WarningItem(OutputModel):
    code: str
    message: str


class Violation(OutputModel):
    id: str
    factor: int = Field(ge=1, le=12)
    rule: str
    file: str
    line: int = Field(ge=1)
    evidence: str
    severity: Literal["info", "warning", "error"] = "warning"
    auto_fixable: bool = False
    change_class: Literal["safe", "risky"] = "safe"
    confidence: Literal["confirmed", "needs_review"] = "needs_review"
    source: Literal["rule", "llm"] = "rule"
    description: str | None = None
    impact: str | None = None


class FactorReview(OutputModel):
    factor: int = Field(ge=1, le=12)
    principle: str
    owner: str
    status: Literal["ok", "violation", "n/a"]


class ReviewCandidate(OutputModel):
    id: str
    factor: Literal[2, 3, 4, 6, 7, 11]
    file: str
    line: int = Field(ge=1)
    evidence: str
    description: str
    source: Literal["llm"] = "llm"
    confidence: Literal["needs_review"] = "needs_review"


class EnvVar(OutputModel):
    name: str
    secret: bool = False
    required: bool = False
    default: str | None = None
    generate: bool = False  # 알려진 템플릿 추출에서만 설정하고, LLM이 설정하지 않는다.


class TransformReport(OutputModel):
    status: Literal["not_run", "proposed", "partial", "failed"] = "not_run"
    changed_files: list[str] = Field(default_factory=list)
    addressed_ids: list[str] = Field(default_factory=list)
    deferred_ids: list[str] = Field(default_factory=list)
    env_vars: list[EnvVar] = Field(default_factory=list)
    migrate_command: str | None = None
    sample_name: str | None = None
    needs_approval: bool = False
    patch_valid: bool = False
    compile_passed: bool = False
    llm_attempts: int = 0
    warnings: list[WarningItem] = Field(default_factory=list)


class Signal(OutputModel):
    name: Literal["scheduler", "websocket", "long_request"]
    file: str
    line: int = Field(ge=1)
    evidence: str


class FrameworkDetection(OutputModel):
    support_grade: Literal["supported", "partial", "unsupported"]
    framework: str | None = None
    entrypoint: str | None = None
    reason: str


class Diagnosis(OutputModel):
    status: Literal["not_implemented", "completed"] = "not_implemented"
    support_grade: Literal["supported", "partial", "unsupported"] | None = None
    violations: list[Violation] = Field(default_factory=list)
    review_candidates: list[ReviewCandidate] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    warnings: list[WarningItem] = Field(default_factory=list)
    framework: FrameworkDetection | None = None
    factor_reviews: list[FactorReview] = Field(default_factory=list)
    enrichment_status: Literal["not_requested", "completed", "failed"] = "not_requested"
    transformation: TransformReport | None = None


class Recommendation(OutputModel):
    status: Literal["not_implemented", "completed", "unsupported"] = "not_implemented"
    set: str | None = None
    rationale: str | None = None
    needs_approval: bool | None = None
    rule_fired: str | None = None
    rationale_source: Literal["rule", "llm"] = "rule"
    signals: list[Signal] = Field(default_factory=list)
    estimated_monthly_cost: float | None = Field(default=None, ge=0)
    assumptions: list[str] = Field(default_factory=list)
    tfvars: dict[str, str | int | bool] = Field(default_factory=dict)
    needs_confirmation: list[str] = Field(default_factory=list)
    warnings: list[WarningItem] = Field(default_factory=list)


class GateReport(OutputModel):
    status: Literal["not_run", "skipped", "passed", "failed"] = "not_run"
    reason: str = "검증 게이트 미구현"
    runner: Literal["none", "fake", "docker"] = "none"
    transformed: GateExecution = Field(default_factory=GateExecution)
    original: GateExecution = Field(default_factory=GateExecution)
    pr_eligible: bool = False
    scope: str = "not_run"
    attempts: list[GateAttempt] = Field(default_factory=list)
    retry_stop_reason: str | None = None
    execution_source: Literal["current_run", "demo_cache"] = "current_run"
    historical_status: str | None = None


class CallCost(OutputModel):
    stage: str
    model_id: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_s: float = Field(default=0, ge=0)


class CostSummary(OutputModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0
    known_cost_usd: float = 0.0
    pricing_complete: bool = True
    unobserved_requests: int = Field(default=0, ge=0)
    usage_complete: bool = True


class CostReport(OutputModel):
    calls: list[CallCost] = Field(default_factory=list)
    stages: dict[str, CostSummary] = Field(default_factory=dict)
    total: CostSummary = Field(default_factory=CostSummary)
    execution_source: Literal["current_run", "llm_replay", "demo_cache"] = "current_run"
    historical: bool = False
    external_calls: int | None = Field(default=None, ge=0)


class AnalysisResult(OutputModel):
    status: Literal["diagnosed", "partial", "unsupported", "failed"] = "diagnosed"
    diagnosis: Diagnosis
    recommendation: Recommendation
    gate_report: GateReport
    cost: CostReport
    output_files: dict[str, str]
    transformation: TransformReport = Field(default_factory=TransformReport)
    deploy_spec: DeploySpec | None = None
    # TemporaryDirectory 소유권은 비공개이고, 게이트에는 직렬화 가능한 루트만 노출한다.
    build_context: BuildContext | None = None
    packaging_warnings: list[WarningItem] = Field(default_factory=list)
    timings_s: dict[str, float] = Field(default_factory=dict)
    execution_source: Literal["current_run", "llm_replay", "demo_cache"] = "current_run"

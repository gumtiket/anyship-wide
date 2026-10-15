import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.resources import files
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, SerializeAsAny, ValidationError

from ai.llm.cost import CostTracker
from ai.models import OutputModel

Tier = Literal["strong", "fast"]


class LLMResult(OutputModel):
    text: str
    parsed: SerializeAsAny[BaseModel] | None = None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    model_id: str
    latency_s: float = Field(default=0, ge=0)
    transport_attempts: int = Field(default=1, ge=0)
    stop_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


class LLMExchange(OutputModel):
    stage: str
    tier: Tier
    schema_attempt: int
    request_system: str
    request_user: str
    request_parameters: dict[str, Any] = Field(default_factory=dict)
    model_id: str | None = None
    response_text: str | None = None
    parsed: Any = None
    status: Literal[
        "request_failed",
        "empty_response",
        "invalid_schema",
        "parsed",
        "text",
        "truncated_response",
        "refused",
    ]
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)
    error_type: str | None = None
    error_code: str | None = None
    error_details: dict[str, Any] = Field(default_factory=dict)
    transport_attempts: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_s: float | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


class LLMError(RuntimeError):
    """소스 코드, 응답 본문, AWS 자격 증명이 없는 공개용 오류."""

    def __init__(self, message: str, *, transport_attempts: int | None = None) -> None:
        self.transport_attempts = transport_attempts
        super().__init__(message)


class SchemaValidationError(LLMError):
    def __init__(self, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(f"LLM JSON/스키마 검증 실패: 총 {attempts}회 호출")


class LLMClient(Protocol):
    def complete(
        self,
        system: str,
        user: str,
        *,
        tier: Tier,
        schema: type[BaseModel] | None,
        stage: str,
    ) -> LLMResult: ...


class ValidatingClient:
    """실제 전송과 가짜 전송이 함께 쓰는 스키마 재시도 반복."""

    def __init__(self, *, tracker: CostTracker | None = None, schema_retries: int = 2) -> None:
        if not 0 <= schema_retries <= 2:
            raise ValueError("JSON 재요청 상한은 0~2회입니다.")
        self.tracker = tracker if tracker is not None else CostTracker()
        self.schema_retries = schema_retries
        self.trace_observer: Callable[[LLMExchange], None] | None = None

    @contextmanager
    def observing(self, observer: Callable[[LLMExchange], None]) -> Iterator[None]:
        previous = self.trace_observer
        self.trace_observer = observer
        try:
            yield
        finally:
            self.trace_observer = previous

    def _observe(self, exchange: LLMExchange) -> None:
        if self.trace_observer is not None:
            self.trace_observer(exchange)

    def _invoke(self, system: str, user: str, *, tier: Tier, stage: str) -> LLMResult:
        raise NotImplementedError

    def request_parameters(self, tier: Tier) -> dict[str, Any]:
        return {}

    def complete(
        self,
        system: str,
        user: str,
        *,
        tier: Tier,
        schema: type[BaseModel] | None = None,
        stage: str,
    ) -> LLMResult:
        if tier not in {"strong", "fast"}:
            raise ValueError("tier는 strong 또는 fast여야 합니다.")
        request_system = system
        if schema is not None:
            request_system += (
                "\n"
                + files("ai").joinpath("prompts/json_output.txt").read_text(encoding="utf-8")
                + "\nJSON Schema: "
                + json.dumps(schema.model_json_schema(), ensure_ascii=False)
            )
        request_user = user
        for attempt in range(self.schema_retries + 1):
            exchange = LLMExchange(
                stage=stage,
                tier=tier,
                schema_attempt=attempt + 1,
                request_system=request_system,
                request_user=request_user,
                request_parameters=self.request_parameters(tier),
                model_id=getattr(self, "models", {}).get(tier),
                status="request_failed",
            )
            try:
                result = self._invoke(request_system, request_user, tier=tier, stage=stage)
            except (LLMError, ValueError) as error:
                exchange.error_type = type(error).__name__
                exchange.error_code = getattr(error, "code", None)
                exchange.error_details = getattr(error, "details", {})
                exchange.transport_attempts = getattr(error, "transport_attempts", None)
                if exchange.transport_attempts != 0:
                    self.tracker.record_unobserved(stage)
                self._observe(exchange)
                raise
            cost = self.tracker.record(
                stage=stage,
                model_id=result.model_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_s=result.latency_s,
                usage=result.usage,
                stop_reason=result.stop_reason,
            )
            result.cost_usd = cost.cost_usd
            result.parsed = None
            exchange.model_id = result.model_id
            exchange.response_text = result.text
            exchange.transport_attempts = result.transport_attempts
            exchange.input_tokens = result.input_tokens
            exchange.output_tokens = result.output_tokens
            exchange.cost_usd = result.cost_usd
            exchange.latency_s = result.latency_s
            exchange.stop_reason = result.stop_reason
            exchange.usage = result.usage
            if result.stop_reason in {"max_tokens", "model_context_window_exceeded"}:
                exchange.status = "truncated_response"
                self._observe(exchange)
                raise LLMError(
                    "LLM 응답이 토큰/컨텍스트 제한으로 중단됐습니다. 자동 재호출하지 않습니다."
                )
            if result.stop_reason in {"refusal", "content_filtered", "guardrail_intervened"}:
                exchange.status = "refused"
                self._observe(exchange)
                raise LLMError("LLM이 요청을 거절했습니다. 자동 재호출하지 않습니다.")
            if not result.text.strip():
                exchange.status = "empty_response"
                self._observe(exchange)
                raise LLMError("LLM이 텍스트 응답을 반환하지 않았습니다.")
            if schema is None:
                exchange.status = "text"
                self._observe(exchange)
                return result
            try:
                raw = result.text.strip()
                if raw.startswith("```") and raw.endswith("```"):
                    raw = "\n".join(raw.splitlines()[1:-1])
                result.parsed = schema.model_validate_json(raw)
                exchange.parsed = result.parsed.model_dump(mode="json")
                exchange.status = "parsed"
                self._observe(exchange)
                return result
            except ValidationError as error:
                # 오류 위치와 유형만 넣고, 거부된 입력이나 응답 본문은 넣지 않는다.
                feedback = [
                    {"loc": list(item["loc"]), "type": item["type"]}
                    for item in error.errors(include_input=False, include_url=False)
                ]
                exchange.status = "invalid_schema"
                exchange.validation_errors = feedback
                self._observe(exchange)
                request_user = (
                    user
                    + "\n이전 출력 검증 오류를 고쳐 다시 응답하세요: "
                    + json.dumps(
                        feedback,
                        ensure_ascii=False,
                    )
                )
                if attempt == self.schema_retries:
                    raise SchemaValidationError(attempt + 1) from None
        raise AssertionError("unreachable")

from ai.llm.base import LLMClient, LLMError, LLMResult
from ai.llm.cost import CostTracker
from ai.models import CallCost, CostReport


class TrackedLLM:
    """이 분석의 사용량만 보여 준다. 공유 클라이언트의 스키마 재시도도 알 수 있으면 포함한다."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client
        tracker = getattr(client, "tracker", None)
        self.source = tracker if isinstance(tracker, CostTracker) else None
        self.start = len(self.source.calls) if self.source is not None else 0
        self.unobserved_start = len(self.source.unobserved_stages) if self.source is not None else 0
        self.fallback = CostTracker()

    def complete(self, *args, **kwargs) -> LLMResult:
        try:
            result = self.client.complete(*args, **kwargs)
        except LLMError:
            if self.source is None:
                self.fallback.record_unobserved(kwargs["stage"])
            raise
        if self.source is None:
            self.fallback.calls.append(
                CallCost(
                    stage=kwargs["stage"],
                    model_id=result.model_id,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd=result.cost_usd,
                    latency_s=result.latency_s,
                )
            )
        return result

    def report(self) -> CostReport:
        if self.source is None:
            return self.fallback.report()
        current = CostTracker()
        current.calls = [call.model_copy() for call in self.source.calls[self.start :]]
        current.unobserved_stages = self.source.unobserved_stages[self.unobserved_start :]
        return current.report()

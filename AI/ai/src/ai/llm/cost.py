import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import Field

from ai.models import CallCost, CostReport, CostSummary, OutputModel


class ModelPricing(OutputModel):
    input_usd_per_million: float | None = Field(default=None, ge=0)
    output_usd_per_million: float | None = Field(default=None, ge=0)
    cache_read_usd_per_million: float | None = Field(default=None, ge=0)
    cache_write_5m_usd_per_million: float | None = Field(default=None, ge=0)
    cache_write_1h_usd_per_million: float | None = Field(default=None, ge=0)
    long_prompt_threshold: int | None = Field(default=None, ge=1)
    long_input_usd_per_million: float | None = Field(default=None, ge=0)
    long_output_usd_per_million: float | None = Field(default=None, ge=0)


class CostTracker:
    def __init__(self, prices: dict[str, ModelPricing] | None = None) -> None:
        self.prices = prices or {}
        self.calls: list[CallCost] = []
        self.unobserved_stages: list[str] = []

    def record_unobserved(self, stage: str) -> None:
        """검증된 사용량을 받기 전에 요청 시도가 실패했다."""
        self.unobserved_stages.append(stage)

    @classmethod
    def from_file(cls, path: str | Path) -> "CostTracker":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            {
                key: ModelPricing.model_validate(value)
                for key, value in data.get("models", {}).items()
            }
        )

    def record(
        self,
        *,
        stage: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_s: float = 0,
        usage: dict[str, Any] | None = None,
        stop_reason: str | None = None,
    ) -> CallCost:
        if usage and usage.get("provider") == "anthropic":
            return self._record_anthropic(
                stage=stage,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_s=latency_s,
                usage=usage,
                stop_reason=stop_reason,
            )
        # 가격을 계산하기 전에 토큰 수를 검증한다.
        call = CallCost(
            stage=stage,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_s=latency_s,
        )
        price = self.prices.get(model_id)
        if (
            price is not None
            and price.input_usd_per_million is not None
            and price.output_usd_per_million is not None
        ):
            call.cost_usd = (
                input_tokens * price.input_usd_per_million
                + output_tokens * price.output_usd_per_million
            ) / 1_000_000
        self.calls.append(call)
        return call

    def _record_anthropic(
        self,
        *,
        stage: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_s: float,
        usage: dict[str, Any],
        stop_reason: str | None,
    ) -> CallCost:
        iterations = usage.get("iterations")
        entries = iterations or [{**usage, "model": model_id}]
        recorded = []
        for index, entry in enumerate(entries):
            last = index == len(entries) - 1
            call = CallCost(
                stage=stage,
                model_id=entry["model"],
                input_tokens=entry["input_tokens"],
                output_tokens=entry["output_tokens"],
                latency_s=latency_s if last else 0,
            )
            call.cost_usd = self._anthropic_price(call, entry)
            if not last and call.output_tokens == 0:
                # 이전 거부 분류는 반복별로 보고되지 않는다. 출력 수가 0이라는 것만으로
                # 과금 여부를 추정할 수 없다.
                call.cost_usd = None
            if last and stop_reason == "refusal" and call.output_tokens == 0:
                category = usage.get("refusal_category")
                if category in {"cyber", "general_harms"} or category is None:
                    call.cost_usd = 0.0
                elif category == "unknown":
                    call.cost_usd = None
            if usage.get("fallback_ran") and not iterations:
                call.cost_usd = None
            recorded.append(call)
        self.calls.extend(recorded)
        return CallCost(
            stage=stage,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_s=latency_s,
            cost_usd=sum(call.cost_usd for call in recorded)
            if all(call.cost_usd is not None for call in recorded)
            else None,
        )

    def _anthropic_price(self, call: CallCost, usage: dict[str, Any]) -> float | None:
        price = self.prices.get(call.model_id)
        if price is None:
            return None
        read = usage.get("cache_read_input_tokens", 0)
        created = usage.get("cache_creation_input_tokens", 0)
        split = usage.get("cache_creation") or {}
        write_5m = split.get("ephemeral_5m_input_tokens", 0)
        write_1h = split.get("ephemeral_1h_input_tokens", 0)
        if created != write_5m + write_1h:
            return None
        long = price.long_prompt_threshold is not None and (
            call.input_tokens + read + created > price.long_prompt_threshold
        )
        # 긴 문맥(long-context) 캐시 단가는 아직 등록하지 않았다.
        if long and (read or created):
            return None
        terms = (
            (
                call.input_tokens,
                price.long_input_usd_per_million if long else price.input_usd_per_million,
            ),
            (
                call.output_tokens,
                price.long_output_usd_per_million if long else price.output_usd_per_million,
            ),
            (read, price.cache_read_usd_per_million),
            (write_5m, price.cache_write_5m_usd_per_million),
            (write_1h, price.cache_write_1h_usd_per_million),
        )
        if any(count and rate is None for count, rate in terms):
            return None
        # 기본 단가가 없으면 토큰이 0이어도 비용은 알 수 없음으로 둔다.
        if terms[0][1] is None or terms[1][1] is None:
            return None
        return sum(count * rate for count, rate in terms if rate is not None) / 1_000_000

    @staticmethod
    def _summary(calls: list[CallCost], unobserved_requests: int = 0) -> CostSummary:
        known = sum(call.cost_usd for call in calls if call.cost_usd is not None)
        complete = all(call.cost_usd is not None for call in calls)
        return CostSummary(
            calls=len(calls),
            input_tokens=sum(c.input_tokens for c in calls),
            output_tokens=sum(c.output_tokens for c in calls),
            known_cost_usd=known,
            cost_usd=known if complete and not unobserved_requests else None,
            pricing_complete=complete,
            unobserved_requests=unobserved_requests,
            usage_complete=not unobserved_requests,
        )

    def report(self) -> CostReport:
        groups: dict[str, list[CallCost]] = defaultdict(list)
        for call in self.calls:
            groups[call.stage].append(call)
        for stage in self.unobserved_stages:
            groups.setdefault(stage, [])
        return CostReport(
            calls=[call.model_copy() for call in self.calls],
            stages={
                stage: self._summary(calls, self.unobserved_stages.count(stage))
                for stage, calls in sorted(groups.items())
            },
            total=self._summary(self.calls, len(self.unobserved_stages)),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.report().model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

"""선택적으로 남기는 마스킹된 모델 주고받기와, 결정적인 전후 비교."""

import json
import re
from typing import Any

from ai.llm.base import LLMExchange
from ai.models import AnalysisResult, Diagnosis
from ai.security import SourceMasker

TRACE_NAMES = ("llm-trace.json", "llm-trace.md", "llm-comparison.json")


def compare(baseline: Diagnosis, final: Diagnosis) -> dict[str, Any]:
    before = {v.id: v.model_dump(mode="json") for v in baseline.violations}
    after = {v.id: v.model_dump(mode="json") for v in final.violations if v.source == "rule"}
    explanations = []
    rule_changes = []
    for identifier in sorted(before.keys() & after.keys()):
        old, new = before[identifier], after[identifier]
        if any(old[field] != new[field] for field in ("description", "impact")):
            explanations.append(
                {
                    "id": identifier,
                    "before": {field: old[field] for field in ("description", "impact")},
                    "after": {field: new[field] for field in ("description", "impact")},
                }
            )
        changed = {
            field: {"before": value, "after": new[field]}
            for field, value in old.items()
            if field not in {"description", "impact"} and value != new[field]
        }
        if changed:
            rule_changes.append({"id": identifier, "changes": changed})
    return {
        "rule_baseline": baseline.model_dump(mode="json"),
        "rule_ids_preserved": before.keys() == after.keys(),
        "rule_field_changes": rule_changes,
        "support_grade_preserved": baseline.support_grade == final.support_grade,
        "signals_preserved": baseline.signals == final.signals,
        "factor_reviews_preserved": baseline.factor_reviews == final.factor_reviews,
        "explanation_changes": explanations,
        "llm_candidates": [v.model_dump(mode="json") for v in final.review_candidates],
        "new_warnings": [
            w.model_dump(mode="json") for w in final.warnings if w not in baseline.warnings
        ],
    }


def fence(text: str, language: str = "") -> str:
    size = max([2, *(len(match) for match in re.findall(r"`+", text))]) + 1
    delimiter = "`" * size
    return f"{delimiter}{language}\n{text}\n{delimiter}\n"


class TraceRecorder:
    def __init__(self, masker: SourceMasker, client_kind: str) -> None:
        self.masker = masker
        self.client_kind = client_kind
        self.exchanges: list[dict[str, Any]] = []

    def sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.masker.text(value)
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        if isinstance(value, dict):
            return {self.sanitize(key): self.sanitize(item) for key, item in value.items()}
        return value

    def record(self, exchange: LLMExchange) -> None:
        original = exchange.model_dump(mode="json")
        safe = self.sanitize(original)
        safe["redacted_for_storage"] = safe != original
        safe["index"] = len(self.exchanges) + 1
        self.exchanges.append(safe)

    def artifacts(self, baseline: Diagnosis, result: AnalysisResult) -> dict[str, str]:
        transmissions = [item["transport_attempts"] for item in self.exchanges]
        summary = {
            "client_kind": self.client_kind,
            "schema_requests": len(self.exchanges),
            "transport_attempts": sum(transmissions)
            if all(value is not None for value in transmissions)
            else None,
            "responses_received": sum(item["response_text"] is not None for item in self.exchanges),
            "parsed_responses": sum(item["status"] == "parsed" for item in self.exchanges),
            "fast_requests": sum(item["tier"] == "fast" for item in self.exchanges),
            "strong_requests": sum(item["tier"] == "strong" for item in self.exchanges),
            "cost": result.cost.model_dump(mode="json"),
        }
        comparison = self.sanitize(compare(baseline, result.diagnosis))
        trace = {
            "format_version": 1,
            "notes": [
                "프롬프트는 JSON Schema와 재시도 피드백을 포함한 실제 complete 요청입니다.",
                "응답 텍스트는 모델 반환 원문이며 인식된 시크릿만 추가 치환합니다. "
                "치환 여부를 표시합니다.",
                "AWS 인증 정보·서명·HTTP 헤더·SDK 원문 오류는 수집하지 않습니다.",
                "transport_attempts는 전송 시도 수이며 "
                "AWS가 수신/과금한 요청 수의 보장이 아닙니다.",
                "cost는 usage를 받은 응답의 집계입니다. "
                "미등록 단가는 null이며 청구 총액이 아닙니다.",
                "strong_requests=0이면 이번 실행에서 STRONG 코드 생성은 검증하지 않았습니다.",
                "시크릿 탐지는 인식 가능한 패턴에 한정하며 모든 패턴의 마스킹을 보장하지 않습니다.",
            ],
            "summary": summary,
            "exchanges": self.exchanges,
        }
        markdown = [
            "# LLM 실행 기록\n",
            "\n".join(f"- {note}" for note in trace["notes"]) + "\n",
            "## 호출·토큰·비용\n"
            + fence(json.dumps(summary, ensure_ascii=False, indent=2), "json"),
            "## 규칙 결과와 비교\n"
            + fence(
                json.dumps(
                    {key: value for key, value in comparison.items() if key != "rule_baseline"},
                    ensure_ascii=False,
                    indent=2,
                ),
                "json",
            ),
        ]
        for item in self.exchanges:
            markdown.append(
                f"## 요청 {item['index']}: {item['stage']} / {item['tier']} / {item['status']}\n"
                + f"\nmodel_id: {item['model_id']}; "
                + f"저장 시 추가 마스킹: {item['redacted_for_storage']}\n"
                + "\n### System 프롬프트\n"
                + fence(item["request_system"], "text")
                + "\n### User 프롬프트 (마스킹된 코드)\n"
                + fence(item["request_user"], "text")
                + "\n### 실제 모델 요청 옵션\n"
                + fence(
                    json.dumps(item["request_parameters"], ensure_ascii=False, indent=2), "json"
                )
                + "\n### 응답 원문 (인식된 시크릿 치환)\n"
                + fence(
                    item["response_text"] if item["response_text"] is not None else "응답 없음",
                    "text",
                )
                + "\n### 파싱 결과\n"
                + fence(json.dumps(item["parsed"], ensure_ascii=False, indent=2), "json")
                + "\n### 종료 사유·원본 usage\n"
                + fence(
                    json.dumps(
                        {"stop_reason": item["stop_reason"], "usage": item["usage"]},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "json",
                )
                + "\n### 검증 오류\n"
                + fence(json.dumps(item["validation_errors"], ensure_ascii=False, indent=2), "json")
                + "\n### 호출 오류\n"
                + fence(
                    json.dumps(
                        {
                            "type": item["error_type"],
                            "code": item["error_code"],
                            "details": item["error_details"],
                        },
                        ensure_ascii=False,
                    ),
                    "json",
                )
            )
        return {
            "llm-trace.json": json.dumps(trace, ensure_ascii=False, indent=2) + "\n",
            "llm-trace.md": "\n".join(markdown),
            "llm-comparison.json": json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
        }

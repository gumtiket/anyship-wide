"""단가 미확인: illustrative planning rates, not AWS prices or a billing quote.

TODO: C must confirm region, resource shapes, actual prices and usage before deployment.
No exchange-rate conversion or free-tier discount is applied.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CostAssumptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    region: str = "ap-northeast-2"
    monthly_requests: int = Field(default=100000, ge=0)
    running_hours: float = Field(default=730.0, ge=0, le=744)
    average_request_seconds: float = Field(default=0.2, gt=0)
    storage_gb: float = Field(default=20.0, ge=0)
    free_tier_excluded: Literal[True] = True


@dataclass(frozen=True)
class Rate:
    value: float | None
    unit: str
    label: str = "단가 미확인"


# 모든 숫자는 일부러 확정하지 않은 계획용 가정값이다. AWS 견적이 아니다.
RATES = {
    "aws-serverless": {
        "requests": Rate(0.20, "USD / 백만 요청"),
        "duration": Rate(0.00002, "USD / GB-second"),
        "database": Rate(0.03, "USD / DB 시간"),
        "storage": Rate(0.15, "USD / GB-month"),
    },
    "aws-always-on": {
        "host": Rate(None, "USD / t3.small 호스트 시간"),
        "database": Rate(None, "USD / RDS db.t4g.micro 시간"),
        "database_storage": Rate(None, "USD / RDS GB-month"),
        "root_volume": Rate(None, "USD / 루트 볼륨 GB-month"),
        "elastic_ip": Rate(None, "USD / 탄력적 IP 시간"),
    },
    "onprem": {"host": Rate(0.015, "USD / 기존 서버 전력·운영 시간")},
}


def estimate_monthly(
    selected: str, *, memory_mb: int, assumptions: CostAssumptions | None = None
) -> tuple[float | None, list[str]]:
    usage = assumptions or CostAssumptions()
    if not usage.free_tier_excluded:
        raise ValueError("free_tier_calculation_not_supported")
    rates = RATES[selected]
    if selected == "aws-serverless":
        amount = (
            usage.monthly_requests / 1000000 * rates["requests"].value
            + usage.monthly_requests
            * usage.average_request_seconds
            * memory_mb
            / 1024
            * rates["duration"].value
            + usage.running_hours * rates["database"].value
            + usage.storage_gb * rates["storage"].value
        )
    elif selected == "aws-always-on":
        amount = None
    else:
        amount = usage.running_hours * rates["host"].value
        if "storage" in rates:
            amount += usage.storage_gb * rates["storage"].value
    notes = [
        "임시 추정치: 단가 미확인, C 확정 전. 실제 AWS 요금이나 청구 견적이 아닙니다.",
        f"리전 가정: {usage.region}"
        if selected != "onprem"
        else "리전 가정: onprem-local (기존 서버)",
        f"월 요청 수 가정: {usage.monthly_requests}",
        f"상시 가동 시간 가정: 월 {usage.running_hours:g}시간",
        "프리티어·크레딧·할인 적용 제외",
        "통화 USD, 환율 변환 미적용",
        f"요청당 평균 실행 시간 가정: {usage.average_request_seconds:g}초 (실측/최대 시간 아님)",
        f"저장 용량 가정: {usage.storage_gb:g}GB, 메모리 입력: {memory_mb}MB",
        "데이터 전송·NAT·백업·로그·세금·환율·DB 고가용성·하드웨어 구입비는 계산하지 않음",
        (
            "환경 공용 기반: 호스트 t3.small + RDS db.t4g.micro 20GB + "
            "루트 볼륨 30GB + 탄력적 IP; 여러 앱이 공유. "
            "앱별 비용이 아님. 단가 미확정으로 총액 산정 보류."
        )
        if selected == "aws-always-on"
        else "서버리스는 소형 단일 DB 가정; 온프레미스는 기존 서버 운영 가정",
    ]
    notes.extend(
        f"{name}: {rate.value if rate.value is not None else '미확정'} {rate.unit} — {rate.label}"
        for name, rate in rates.items()
    )
    return round(amount, 4) if amount is not None else None, notes

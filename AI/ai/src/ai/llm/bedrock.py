import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ai.llm.base import LLMError, LLMResult, Tier, ValidatingClient
from ai.llm.cost import CostTracker

TRANSIENT_CODES = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelNotReadyException",
    }
)


def classify_denial(message: str) -> dict[str, Any]:
    """고정된 사유 플래그와 동작 이름만 돌려준다. SDK 메시지나 자격 증명은 돌려주지 않는다."""
    text = message.lower()
    return {
        "reported_actions": sorted(
            set(re.findall(r"(?<![\w:])(?:bedrock|aws-marketplace):[A-Za-z]+", message))
        ),
        "marketplace": "marketplace" in text,
        "subscription": any(word in text for word in ("subscription", "subscribe", "subscribed")),
        "agreement": any(word in text for word in ("agreement", "eula")),
        "use_case": any(word in text for word in ("use case", "usecase", "first time")),
        "payment": any(word in text for word in ("payment", "billing")),
        "identity_policy": "identity-based policy" in text,
        "explicit_deny": "explicit deny" in text,
        "permissions_boundary": "permissions boundary" in text,
        "organization_policy": any(word in text for word in ("service control policy", "scp")),
        "account_restricted": any(
            word in text
            for word in ("contact aws", "not available for your account", "not eligible")
        ),
    }


class BedrockCallError(LLMError):
    def __init__(
        self, code: str, *, transport_attempts: int = 0, details: dict[str, Any] | None = None
    ) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(f"Bedrock 호출 실패 ({code})", transport_attempts=transport_attempts)


class BedrockClient(ValidatingClient):
    def __init__(
        self,
        *,
        client: Any = None,
        environ: Mapping[str, str] | None = None,
        tracker: CostTracker | None = None,
        schema_retries: int = 2,
        transport_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_tokens: int = 4096,
    ) -> None:
        env = os.environ if environ is None else environ
        required = ("BEDROCK_REGION", "BEDROCK_MODEL_ID_STRONG", "BEDROCK_MODEL_ID_FAST")
        missing = [name for name in required if not env.get(name, "").strip()]
        if missing:
            raise ValueError("필수 환경변수 누락: " + ", ".join(missing))
        if not 1 <= transport_attempts <= 5:
            raise ValueError("전송 시도 상한은 1~5회입니다.")
        if max_tokens < 1:
            raise ValueError("max_tokens는 양수여야 합니다.")
        if tracker is None:
            # 편집 가능 설치(editable)한 소스 기준이며, 외부 프로젝트의 절대 경로는 쓰지 않는다.
            pricing_path = Path(__file__).resolve().parents[3] / "pricing.json"
            tracker = (
                CostTracker.from_file(pricing_path) if pricing_path.is_file() else CostTracker()
            )
        super().__init__(tracker=tracker, schema_retries=schema_retries)
        self.region = env["BEDROCK_REGION"].strip()
        self.models = {
            "strong": env["BEDROCK_MODEL_ID_STRONG"].strip(),
            "fast": env["BEDROCK_MODEL_ID_FAST"].strip(),
        }
        self.client = client
        self.transport_attempts = transport_attempts
        self.sleep = sleep
        self.clock = clock
        self.max_tokens = max_tokens

    def request_parameters(self, tier: Tier) -> dict[str, Any]:
        # ID는 기본 ID, 리전·전역 추론 프로필 ID, 전체 리소스 ARN 중 하나일 수 있다.
        family = self.models[tier].rsplit("/", 1)[-1].rsplit(".", 1)[-1]
        inference: dict[str, Any] = {"maxTokens": self.max_tokens}
        request: dict[str, Any] = {"inferenceConfig": inference}
        if family in {"claude-sonnet-5-5", "claude-haiku-5-5"}:
            # 5.5는 기본값이 아닌 샘플링 설정을 거부한다. 이 작업 흐름에는 도구 반복이 없으므로
            # 사전 thinking이 작은 확인·JSON 출력 예산을 써 버리지 않게 한다.
            request["additionalModelRequestFields"] = {
                "thinking": {
                    "type": "between_tools" if family == "claude-sonnet-5-5" else "disabled"
                },
                "output_config": {"effort": "medium" if tier == "strong" else "low"},
            }
        else:
            inference["temperature"] = 0
        return request

    def _invoke(self, system: str, user: str, *, tier: Tier, stage: str) -> LLMResult:
        started = self.clock()
        transmissions = 0
        try:
            if self.client is None:
                # 로그인 갱신은 별도의 signin 클라이언트를 만든다. 프로필에 리전이 없을 때를 위해
                # 바깥 Bedrock 클라이언트뿐 아니라 세션 자체에도 리전을 지정한다.
                session = boto3.Session(region_name=self.region)
                self.client = session.client(
                    "bedrock-runtime",
                    region_name=self.region,
                    config=Config(
                        retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=60
                    ),
                )
            for attempt in range(self.transport_attempts):
                try:
                    transmissions += 1
                    response = self.client.converse(
                        modelId=self.models[tier],
                        system=[{"text": system}],
                        messages=[{"role": "user", "content": [{"text": user}]}],
                        **self.request_parameters(tier),
                    )
                    break
                except ClientError as error:
                    code = error.response.get("Error", {}).get("Code", "Unknown")
                    if code not in TRANSIENT_CODES or attempt == self.transport_attempts - 1:
                        details = (
                            classify_denial(error.response.get("Error", {}).get("Message", ""))
                            if code == "AccessDeniedException"
                            else {}
                        )
                        raise BedrockCallError(
                            code, transport_attempts=transmissions, details=details
                        ) from None
                    self.sleep(2**attempt)
        except BotoCoreError as error:
            # 시간 초과여도 추론은 끝났을 수 있다. 유료 호출을 무작정 재시도하지 않는다.
            raise BedrockCallError(type(error).__name__, transport_attempts=transmissions) from None
        try:
            usage = response["usage"]
            text = "".join(
                block.get("text", "") for block in response["output"]["message"]["content"]
            )
            return LLMResult(
                text=text,
                input_tokens=usage["inputTokens"],
                output_tokens=usage["outputTokens"],
                model_id=self.models[tier],
                latency_s=max(0, self.clock() - started),
                transport_attempts=transmissions,
                stop_reason=response.get("stopReason"),
                usage=usage,
            )
        except (KeyError, TypeError, ValueError):
            raise LLMError(
                "Bedrock 응답 형식/usage가 유효하지 않습니다.",
                transport_attempts=transmissions,
            ) from None

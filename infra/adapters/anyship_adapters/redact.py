"""어댑터 밖으로 나가는 모든 것에서 비밀 값을 걸러낸다.

로그 이벤트와 결과에 두 겹으로 적용한다.
  1. 이번 배포의 비밀 값 자체를 나타나는 모든 곳에서 바꾼다.
  2. 널리 알려진 비밀의 모양(클라우드 키, 토큰, 개인 키, URL 안의 비밀번호)은
     어댑터가 모르는 값이어도 바꾼다.

안전망일 뿐이다. 어댑터는 처음부터 메시지에 비밀을 넣지 않아야 한다.
"""

import re
from collections.abc import Mapping
from typing import TypeVar

from pydantic import JsonValue

from .base import LogFn
from .models import AdapterModel, LogEvent

MASK = "***"

# 이보다 짧은 값을 가리면 관계없는 글자까지 지워 버린다. 이렇게 짧은 비밀은
# 쓰지 말아야 한다.
MIN_SECRET_LENGTH = 4

# 소스에 개인 키 머리글이 그대로 보이지 않도록 나눠서 만든다(비밀 스캐너 오탐 방지).
_PRIVATE_KEY_BLOCK = (
    r"-----BEGIN [A-Z ]*" + r"PRIVATE KEY" + r"-----.*?-----END [A-Z ]*" + r"PRIVATE KEY" + r"-----"
)

_PATTERNS = (
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS 액세스 키 ID
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub 토큰
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(_PRIVATE_KEY_BLOCK, re.DOTALL),  # PEM 개인 키
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),  # URL 안의 사용자:비밀번호
)

ModelT = TypeVar("ModelT", bound=AdapterModel)


def redact_text(text: str, secrets: Mapping[str, str] | None = None) -> str:
    # 긴 값부터 처리해야 비밀의 뒷부분이 남지 않는다.
    for value in sorted(
        {v for v in (secrets or {}).values() if len(v) >= MIN_SECRET_LENGTH}, key=len, reverse=True
    ):
        text = text.replace(value, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(MASK, text)
    return text


def redact_json(value: JsonValue, secrets: Mapping[str, str] | None = None) -> JsonValue:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_json(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact_json(item, secrets) for key, item in value.items()}
    return value


def redact_model(model: ModelT, secrets: Mapping[str, str] | None = None) -> ModelT:
    """모든 문자열을 걸러낸 복사본을 돌려준다. 원본은 바꾸지 않는다."""
    data = redact_json(model.model_dump(mode="json"), secrets)
    return type(model).model_validate(data)


def redact_event(event: LogEvent, secrets: Mapping[str, str] | None = None) -> LogEvent:
    return redact_model(event, secrets)


def make_safe_log(log: LogFn, secrets: Mapping[str, str] | None = None) -> LogFn:
    """받는 어떤 이벤트에도 비밀이 담길 수 없도록 로그 함수를 감싼다."""

    def safe(event: LogEvent) -> None:
        log(redact_event(event, secrets))

    return safe

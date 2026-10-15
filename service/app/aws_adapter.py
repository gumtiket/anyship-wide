"""Service-facing AWS adapter contract. No AWS SDK or credentials are needed here."""

from dataclasses import dataclass
from typing import Protocol

ERRORS = {
    "aws_adapter_unavailable": (
        503,
        "연결 확인 기능을 준비 중입니다. 입력한 Role ARN은 저장되었습니다.",
    ),
    "access_denied": (
        403,
        "역할 ARN, External ID 및 양쪽 계정의 신뢰 정책·AssumeRole 권한을 확인해 주세요.",
    ),
    "external_id_not_required": (
        422,
        "역할 신뢰 정책이 올바른 External ID를 필수로 요구하도록 수정해 주세요.",
    ),
    "account_mismatch": (422, "검증한 AWS 계정과 역할 ARN의 계정이 일치하지 않습니다."),
    "service_credentials_unavailable": (503, "서비스 서버의 AWS 인증 설정을 확인해야 합니다."),
    "aws_unavailable": (503, "AWS 검증을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요."),
    "invalid_aws_response": (502, "AWS 역할 검증 응답을 확인할 수 없습니다. 다시 시도해 주세요."),
}


class AwsCheckError(Exception):
    def __init__(self, code):
        self.code = code if code in ERRORS else "aws_unavailable"
        self.status_code, self.message = ERRORS[self.code]
        super().__init__(self.message)


@dataclass(frozen=True)
class AwsIdentity:
    account_id: str


class AwsAdapter(Protocol):
    """Return a verified identity or raise AwsCheckError; never return AWS credentials."""

    def check(self, *, role_arn: str, external_id: str, region: str) -> AwsIdentity: ...

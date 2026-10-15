"""실제 배포가 AWS 환경 레코드와 주고받는 값: `env_id`, 공용 기반 값, `state_bucket`.

서비스 DB의 환경 레코드(`AwsEnvironment`)와 어댑터의 `AwsEnvironment` 사이를 잇는다. 여기의 값은 모두 비밀이 아니다
(비밀번호는 Secrets Manager에만 있고, 사용자의 External ID는 어댑터 모델로 넘기기만 한다). AWS는 직접 부르지 않고
`access`(`AwsAccess`)를 받아서 쓰므로 시험에서는 가짜를 끼운다.

각 함수는 값을 바꾸면 곧바로 커밋한다. 호출하는 쪽의 다른 트랜잭션과 섞지 않는다.
"""

import uuid

from pydantic import ValidationError

from anyship_adapters import AwsEnvironment as AdapterEnvironment

from .db import AwsEnvironment

FOUNDATION_FIELDS = ("host", "db_address", "db_port", "db_secret_arn")
_OPTIONAL = FOUNDATION_FIELDS + ("state_bucket",)


class DeployStateError(Exception):
    """배포에 쓸 수 없는 환경이거나 저장할 값이 올바르지 않을 때. 문구에는 값을 싣지 않는다."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def assign_env_id(session, row: AwsEnvironment) -> str:
    """`env_id`가 없으면 환경 레코드 ID에서 정한다(`e` + 20자 = 21자). 이미 있으면 바꾸지 않는다(예: 먼저 만든 `test`)."""
    if row.env_id is None:
        row.env_id = "e" + uuid.UUID(row.id).hex[:20]
        session.commit()
    return row.env_id


def adapter_environment(row: AwsEnvironment) -> AdapterEnvironment:
    """어댑터에 넘길 환경. 연결이 확인된(`CONNECTED`) 환경만 만든다. 모의 배포와 달리 저장만 한 ARN으로 대신하지 않는다."""
    if row.status != "CONNECTED" or not row.role_arn:
        raise DeployStateError(
            "environment_not_connected", "AWS 연결이 확인된 환경만 배포에 사용할 수 있습니다."
        )
    if row.env_id is None:
        raise DeployStateError("env_id_missing", "환경의 배포용 식별자가 아직 정해지지 않았습니다.")
    optional = {name: getattr(row, name) for name in _OPTIONAL if getattr(row, name) is not None}
    try:
        return AdapterEnvironment(
            env_id=row.env_id,
            role_arn=row.role_arn,
            external_id=row.external_id,
            region=row.region,
            **optional,
        )
    except ValidationError:  # 오류에 값이 담기므로 그대로 올리지 않는다
        raise DeployStateError(
            "environment_invalid", "저장된 환경 정보가 배포 형식에 맞지 않습니다."
        ) from None


def ensure_state_bucket(session, row: AwsEnvironment, access) -> str:
    """`state_bucket`이 비어 있으면 온보딩 스택의 출력에서 읽어 저장한다. 읽기 실패는 `AwsAccessError`로 올라간다."""
    if row.state_bucket:
        return row.state_bucket
    row.state_bucket = access.read_state_bucket(adapter_environment(row), row.stack_name)
    session.commit()
    return row.state_bucket


def save_foundation(session, row: AwsEnvironment, fields) -> None:
    """배포가 성공하면 결과의 `details["foundation"]`(주소와 비밀의 ARN 4개)을 저장한다. 어댑터 모델의 규칙을 통과해야 한다."""
    if set(fields) != set(FOUNDATION_FIELDS):
        raise DeployStateError("foundation_invalid", "기반 값의 항목이 올바르지 않습니다.")
    base = adapter_environment(row).model_dump(exclude=set(FOUNDATION_FIELDS))
    try:
        AdapterEnvironment(**{**base, **fields})
    except ValidationError:
        raise DeployStateError(
            "foundation_invalid", "기반 값이 올바르지 않거나 이 계정의 것이 아닙니다."
        ) from None
    for name in FOUNDATION_FIELDS:
        setattr(row, name, fields[name])
    session.commit()

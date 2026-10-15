"""사용자 AWS 계정의 역할을 AssumeRole해서 필요한 값을 읽는다(aws-always-on 어댑터용).

하는 일은 이렇다.
  * check_role: 역할을 맡을 수 있고, 그 계정이 역할 ARN의 계정과 같은지 확인한다.
  * read_master_password: 공용 RDS의 마스터 비밀번호를 Secrets Manager에서 읽는다.
  * read_state_bucket: 온보딩 스택의 출력에서 Terraform state 버킷 이름을 읽는다.
  * temporary_credentials: Terraform 하위 프로세스에 환경변수로만 넘길 임시 자격 증명을 받는다.

임시 자격 증명은 이 모듈 안에서만 쓰고 밖으로 돌려주지 않는다. 비밀번호는 읽은 쪽이 곧바로
SSH 표준입력으로만 쓰고, 로그와 결과에 남기지 않는다(호출하는 쪽이 redact에 등록한다).

External ID가 없을 때 거부되는지 같은 신뢰 정책 검증은 환경 등록 단계(서비스의 STSAdapter)에서
이미 한다. 배포마다 반복하지 않고 정상 경로만 확인한다.

boto3는 선택 의존성이라 처음 쓸 때 불러온다.
"""

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .models import AdapterError, AwsEnvironment


class AwsAccessError(Exception):
    """AWS에 접근할 수 없을 때 던진다. 어댑터가 잡아서 결과의 오류로 바꾼다."""

    def __init__(self, error: AdapterError):
        super().__init__(error.message)
        self.error = error


def _fail(
    code: str, message: str, hint: str | None = None, retryable: bool = False
) -> AwsAccessError:
    return AwsAccessError(AdapterError(code=code, message=message, hint=hint, retryable=retryable))


def _account_of(arn: str) -> str:
    return arn.split(":")[4]


_STACK = re.compile(r"^[A-Za-z][-A-Za-z0-9]{0,127}$")  # CloudFormation 스택 이름 규칙
_STACK_READY = ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE")


def _stack_not_found() -> AwsAccessError:
    return _fail(
        "stack_not_found",
        "온보딩 스택을 찾을 수 없습니다.",
        hint="스택 이름을 바꾸지 않았는지, 올바른 리전인지 확인해 주세요.",
    )


def _aws_error(exc: Exception) -> AwsAccessError:
    # botocore 예외 종류가 많아 이름으로 나눈다. 원문(ARN 등)은 메시지에 담지 않는다.
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    if code == "AccessDenied":
        return _fail(
            "access_denied",
            "역할을 맡을 수 없습니다.",
            hint="역할 ARN, External ID, 사용자 계정의 신뢰 정책을 확인해 주세요.",
        )
    if code in ("ExpiredToken", "InvalidClientTokenId", "SignatureDoesNotMatch") or type(
        exc
    ).__name__ in ("NoCredentialsError", "PartialCredentialsError"):
        return _fail(
            "service_credentials_unavailable", "서비스 서버의 AWS 인증 설정을 확인해야 합니다."
        )
    return _fail("aws_unavailable", "AWS에 연결하지 못했습니다.", retryable=True)


@dataclass(frozen=True)
class TemporaryCredentials:
    """하위 프로세스(Terraform)에 환경변수로만 전달하는 임시 자격 증명. repr과 str에는 값이 나오지 않는다."""

    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str = field(repr=False)

    def environ(self) -> dict[str, str]:
        return {
            "AWS_ACCESS_KEY_ID": self.access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.secret_access_key,
            "AWS_SESSION_TOKEN": self.session_token,
        }

    def secret_values(self) -> dict[str, str]:
        """redact에 등록할 값. 키 이름은 아무 의미가 없고 값만 쓰인다."""
        return {
            "access_key_id": self.access_key_id,
            "secret_access_key": self.secret_access_key,
            "session_token": self.session_token,
        }


class AwsAccess:
    def __init__(self, session_factory: Callable[..., Any] | None = None):
        # 인자 없이 부르면 서비스 서버의 인스턴스 역할 자격 증명, 자격 증명 인자를 주면 그 자격 증명의 세션.
        # 시험에서는 가짜 공장을 끼워 넣는다.
        self._session_factory = session_factory

    def _session(self, **credentials: str) -> Any:
        if self._session_factory:
            return self._session_factory(**credentials)
        import boto3

        return boto3.Session(**credentials)

    def _assume_role(self, env: AwsEnvironment, duration_seconds: int) -> dict[str, str]:
        """STS로 역할을 맡아 임시 자격 증명을 받는다. 이 모듈 안에서만 쓴다."""
        try:
            sts = self._session().client("sts", region_name=env.region)
            return sts.assume_role(
                RoleArn=env.role_arn,
                ExternalId=env.external_id,
                DurationSeconds=duration_seconds,
                RoleSessionName="anyship-" + uuid.uuid4().hex[:16],
            )["Credentials"]
        except Exception as exc:
            raise _aws_error(exc) from None

    def _assume(self, env: AwsEnvironment) -> Any:
        """역할을 맡아 그 자격 증명으로 만든 세션을 돌려준다. 자격 증명 값은 밖으로 나가지 않는다."""
        creds = self._assume_role(env, 900)
        try:
            return self._session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=env.region,
            )
        except Exception as exc:
            raise _aws_error(exc) from None

    def temporary_credentials(
        self, env: AwsEnvironment, duration_seconds: int = 3600
    ) -> TemporaryCredentials:
        """Terraform 같은 하위 프로세스의 환경변수로만 쓸 임시 자격 증명. 이 모듈에서 자격 증명이 나가는 유일한 통로다.

        받은 쪽은 environ()을 프로세스 환경에만 넣고, 로그와 결과에 남기지 않는다. 오류 문구와 출력에서 값을 가리려면
        secret_values()를 redact 대상에 등록한다. 시간은 15분~1시간(서비스 서버의 인스턴스 역할이 다른 역할을 맡는
        경우의 상한)이다."""
        if not 900 <= duration_seconds <= 3600:
            raise ValueError("duration_seconds must be between 900 and 3600")
        creds = self._assume_role(env, duration_seconds)
        try:
            return TemporaryCredentials(
                creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"]
            )
        except (KeyError, TypeError):
            raise _fail(
                "aws_unavailable", "AWS의 임시 자격 증명 응답을 읽지 못했습니다.", retryable=True
            ) from None

    def check_role(self, env: AwsEnvironment) -> str:
        """역할을 맡을 수 있으면 계정 ID를 돌려준다. 역할 ARN의 계정과 다르면 실패한다."""
        session = self._assume(env)
        try:
            account = session.client("sts").get_caller_identity()["Account"]
        except Exception:
            raise _fail(
                "aws_unavailable", "맡은 역할로 계정을 확인하지 못했습니다.", retryable=True
            ) from None
        if account != _account_of(env.role_arn):
            raise _fail(
                "account_mismatch", "검증한 AWS 계정과 역할 ARN의 계정이 일치하지 않습니다."
            )
        return account

    def read_state_bucket(self, env: AwsEnvironment, stack_name: str) -> str:
        """온보딩 스택의 출력 `StateBucketName`을 읽는다(이름은 스택 ID로 정해져 계산할 수 없다).

        서비스가 환경을 처음 배포하기 전에 한 번 읽어 저장한다. 스택 이름은 서비스가 만든 값이지만 사용자가 콘솔에서
        바꿀 수 있으므로 스택이 없는 경우를 따로 알린다. 읽은 값은 모델의 `state_bucket` 규칙(형식, 같은 계정)을
        통과해야 한다."""
        if not _STACK.match(stack_name):
            raise _fail("invalid_stack_name", "온보딩 스택 이름이 올바르지 않습니다.")
        session = self._assume(env)
        try:
            stacks = session.client("cloudformation", region_name=env.region).describe_stacks(
                StackName=stack_name
            )["Stacks"]
        except Exception as exc:
            if (
                getattr(exc, "response", {}).get("Error", {}).get("Code") == "ValidationError"
            ):  # 없는 스택
                raise _stack_not_found() from None
            raise _aws_error(exc) from None
        if not stacks:
            raise _stack_not_found()
        stack = stacks[0]
        if stack.get("StackStatus") not in _STACK_READY:
            raise _fail(
                "stack_not_ready",
                "온보딩 스택이 아직 완료되지 않았거나 실패한 상태입니다.",
                hint="AWS 콘솔에서 스택이 CREATE_COMPLETE인지 확인해 주세요.",
                retryable=True,
            )
        bucket = next(
            (
                o.get("OutputValue")
                for o in stack.get("Outputs", [])
                if o.get("OutputKey") == "StateBucketName"
            ),
            None,
        )
        try:
            if not isinstance(bucket, str):
                raise ValueError
            return AwsEnvironment.model_validate(
                {**env.model_dump(), "state_bucket": bucket}
            ).state_bucket
        except (
            ValueError
        ):  # pydantic의 ValidationError도 ValueError다. 읽은 값은 오류에 싣지 않는다.
            raise _fail(
                "stack_output_invalid",
                "온보딩 스택의 StateBucketName 출력을 읽지 못했습니다.",
                hint="스택이 서비스의 최신 템플릿으로 만들어졌는지 확인해 주세요.",
            ) from None

    def read_master_password(self, env: AwsEnvironment) -> str:
        """공용 RDS의 마스터 비밀번호. env.db_secret_arn이 같은 계정의 비밀이어야 한다."""
        arn = env.db_secret_arn
        if arn is None or _account_of(arn) != _account_of(env.role_arn):
            raise _fail(
                "invalid_secret_arn",
                "DB 비밀의 ARN이 없거나 이 계정의 것이 아닙니다.",
                hint="공용 기반이 만들어졌는지, 환경 정보의 db_secret_arn을 확인해 주세요.",
            )
        session = self._assume(env)
        try:
            client = session.client("secretsmanager", region_name=arn.split(":")[3])
            password = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])["password"]
        except Exception:
            raise _fail(
                "secret_unreadable",
                "DB 마스터 비밀번호를 읽지 못했습니다.",
                hint="역할에 Secrets Manager 읽기 권한이 있는지 확인해 주세요.",
                retryable=True,
            ) from None
        if not isinstance(password, str) or not password or "\n" in password or "\r" in password:
            raise _fail("secret_unreadable", "DB 마스터 비밀번호 형식이 올바르지 않습니다.")
        return password

"""명시적으로 켤 때만 쓰는 STS 참조 어댑터. 서비스 진입점은 이를 자동으로 고르지 않는다."""

import uuid
from contextlib import ExitStack

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
)

from .aws_adapter import AwsCheckError, AwsIdentity
from .aws_validation import role_account_id


class STSAdapter:
    def check(self, *, role_arn: str, external_id: str, region: str) -> AwsIdentity:
        expected = role_account_id(role_arn)
        config = Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"mode": "standard", "total_max_attempts": 2},
            ignore_configured_endpoint_urls=True,
        )
        try:
            # 요청 스레드끼리 boto3 Session을 공유하지 않도록 매번 새 세션을 만든다.
            session = boto3.Session()
            with ExitStack() as cleanup:
                sts = session.client("sts", region_name=region, config=config)
                cleanup.callback(sts.close)
                parameters = {
                    "RoleArn": role_arn,
                    "RoleSessionName": "anyship-check-" + uuid.uuid4().hex,
                    "DurationSeconds": 900,
                }
                credentials = sts.assume_role(**parameters, ExternalId=external_id)["Credentials"]
                # 명시적인 AccessDenied만이 이 거부 확인 호출이 의도대로 실패했다는 증거다.
                for extra in ({}, {"ExternalId": "invalid-" + uuid.uuid4().hex}):
                    try:
                        sts.assume_role(**parameters, **extra)
                    except ClientError as error:
                        if error.response.get("Error", {}).get("Code") != "AccessDenied":
                            raise
                    else:
                        raise AwsCheckError("external_id_not_required")
                assumed = session.client(
                    "sts",
                    region_name=region,
                    config=config,
                    aws_access_key_id=credentials["AccessKeyId"],
                    aws_secret_access_key=credentials["SecretAccessKey"],
                    aws_session_token=credentials["SessionToken"],
                )
                cleanup.callback(assumed.close)
                account_id = assumed.get_caller_identity()["Account"]
                if account_id != expected:
                    raise AwsCheckError("account_mismatch")
                return AwsIdentity(account_id=account_id)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "AccessDenied":
                raise AwsCheckError("access_denied") from None
            if code in ("ExpiredToken", "InvalidClientTokenId", "SignatureDoesNotMatch"):
                raise AwsCheckError("service_credentials_unavailable") from None
            raise AwsCheckError("aws_unavailable") from None
        except (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError):
            raise AwsCheckError("service_credentials_unavailable") from None
        except BotoCoreError:
            raise AwsCheckError("aws_unavailable") from None
        except (KeyError, TypeError):
            raise AwsCheckError("invalid_aws_response") from None

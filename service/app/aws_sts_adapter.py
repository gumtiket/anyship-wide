"""Explicit opt-in STS reference adapter; the Service entrypoint never selects it."""

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
            # A fresh session avoids sharing boto3 Sessions between request threads.
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
                # Only an explicit AccessDenied proves these negative probes failed as required.
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

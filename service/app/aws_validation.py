"""AWS onboarding input rules; MVP supports the commercial aws partition."""

import re
from urllib.parse import urlsplit

ROLE_ARN = re.compile(r"arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9_+=,.@/-]{1,512})")
REGION = re.compile(r"[a-z]{2}-[a-z]+(?:-[a-z]+)?-[0-9]+")


def role_account_id(arn: str) -> str:
    match = ROLE_ARN.fullmatch(arn)
    if (
        not match
        or not match[2].split("/")[-1]
        or len(match[2].split("/")[-1]) > 64
        or "//" in match[2]
    ):
        raise ValueError("Expected an IAM role ARN in the aws partition.")
    return match[1]


def valid_region(region: str) -> bool:
    return bool(REGION.fullmatch(region)) and not region.startswith(("cn-", "us-gov-", "us-iso"))


def validate_aws_settings(template_url: str, service_role_arn: str, regions: tuple[str, ...]):
    if template_url:
        parsed = urlsplit(template_url)
        # Regional S3 URLs from the quick-create contract, plus the global S3 endpoint.
        host = parsed.hostname or ""
        s3_host = re.fullmatch(
            r"(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", host
        )
        if (
            parsed.scheme != "https"
            or not s3_host
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.fragment
            or parsed.path in ("", "/")
            or any(c.isspace() for c in template_url)
        ):
            raise ValueError("APP_AWS_TEMPLATE_URL must be an HTTPS S3 template URL.")
    if service_role_arn:
        role_account_id(service_role_arn)
    if any(not valid_region(region) for region in regions):
        raise ValueError("APP_AWS_REGIONS must contain commercial AWS region codes.")

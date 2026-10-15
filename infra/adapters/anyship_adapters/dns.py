"""Route 53에 환경 단위 와일드카드 레코드를 만들고 지운다(온프레미스와 AWS 호스트용).

레코드는 앱마다가 아니라 **환경(서버)마다 하나**다: `*.<환경ID>.<범위>.<도메인>` -> 서버의 공인 IP.
범위(scope)는 `onprem`(사용자 서버) 또는 `aws`(사용자 AWS 계정의 호스트)이고, 기본은 `onprem`이다.
앱을 더 배포해도 DNS는 그대로이고, 환경을 삭제할 때만 지운다.

boto3는 선택 의존성이라 처음 쓸 때 불러온다(이 모듈만 불러서는 boto3가 없어도 문제없다).
AWS 자격 증명은 서비스 서버의 인스턴스 역할이 제공한다. 이 모듈은 비밀을 다루지 않는다.
"""

import ipaddress
import re
from typing import Any

from .models import ENV_ID_PATTERN, AdapterError

_ENV_ID = re.compile(ENV_ID_PATTERN)
SCOPES = (
    "onprem",
    "aws",
)  # 서비스 서버 역할의 Route 53 정책(infra/service-account/dns.tf)이 허용하는 이름 범위와 같아야 한다


class DnsError(Exception):
    """DNS 작업을 할 수 없을 때 던진다. 어댑터가 잡아서 결과의 오류로 바꾼다."""

    def __init__(self, error: AdapterError):
        super().__init__(error.message)
        self.error = error


def _fail(code: str, message: str, hint: str | None = None, retryable: bool = False) -> DnsError:
    return DnsError(AdapterError(code=code, message=message, hint=hint, retryable=retryable))


class WildcardRecords:
    def __init__(
        self,
        client: Any = None,
        *,
        base_domain: str = "anyship.cloud",
        ttl: int = 60,
        scope: str = "onprem",
    ):
        if scope not in SCOPES:
            raise ValueError("scope must be one of " + ", ".join(SCOPES))
        self._client = client  # 시험에서는 가짜 클라이언트를 끼워 넣는다
        self._scope = scope
        self._domain = base_domain
        self._ttl = ttl
        self._zone: str | None = None

    def name_for(self, env_id: str) -> str:
        return f"*.{env_id}.{self._scope}.{self._domain}"

    def ensure(self, env_id: str, ip: str) -> bool:
        """레코드를 이 IP로 맞춘다(멱등). 바꿨으면 True, 이미 같으면 False."""
        self._check_env(env_id)
        self._check_ip(ip)
        current = self._current(env_id)
        if (
            current
            and [r["Value"] for r in current.get("ResourceRecords", [])] == [ip]
            and current.get("TTL") == self._ttl
        ):
            return False
        record = {
            "Name": self.name_for(env_id),
            "Type": "A",
            "TTL": self._ttl,
            "ResourceRecords": [{"Value": ip}],
        }
        self._change("UPSERT", record)
        return True

    def remove(self, env_id: str) -> bool:
        """레코드를 지운다(멱등). 지웠으면 True, 원래 없었으면 False."""
        self._check_env(env_id)
        current = self._current(env_id)
        if not current:
            return False
        self._change("DELETE", current)  # 지울 때는 서버가 돌려준 레코드를 그대로 넘겨야 한다
        return True

    # -- 내부 ---------------------------------------------------------------------------
    def _check_env(self, env_id: str) -> None:
        if not _ENV_ID.match(env_id):
            raise _fail("invalid_dns_input", "환경 ID 형식이 올바르지 않습니다.")

    def _check_ip(self, ip: str) -> None:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            address = None
        if address is None or address.version != 4 or not address.is_global:
            raise _fail(
                "invalid_dns_input",
                "공인 IPv4 주소만 DNS에 등록할 수 있습니다.",
                hint="서버의 공개 IP를 확인해 주세요(사설 주소나 루프백은 사용할 수 없습니다).",
            )

    def _api(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise _fail(
                    "dns_unavailable",
                    "boto3가 설치되어 있지 않아 DNS 레코드를 바꿀 수 없습니다.",
                    hint='서비스 서버에서 `pip install "anyship-adapters[aws]"`로 설치해 주세요.',
                ) from None
            self._client = boto3.client("route53")
        return self._client

    def _zone_id(self) -> str:
        if self._zone is None:
            found = self._call(
                lambda api: api.list_hosted_zones_by_name(DNSName=f"{self._domain}.", MaxItems="1")
            )
            zones = [
                z
                for z in found.get("HostedZones", [])
                if z["Name"] == f"{self._domain}." and not z.get("Config", {}).get("PrivateZone")
            ]
            if not zones:
                raise _fail(
                    "dns_zone_not_found",
                    f"{self._domain}의 호스팅 영역을 찾을 수 없습니다.",
                    hint="Route 53에 이 도메인의 공개 호스팅 영역이 있는지 확인해 주세요.",
                )
            self._zone = zones[0]["Id"].rsplit("/", 1)[-1]
        return self._zone

    def _current(self, env_id: str) -> dict | None:
        # Route 53은 와일드카드(*)를 \052로 돌려준다.
        wanted = f"\\052.{env_id}.{self._scope}.{self._domain}."
        found = self._call(
            lambda api: api.list_resource_record_sets(
                HostedZoneId=self._zone_id(),
                StartRecordName=wanted,
                StartRecordType="A",
                MaxItems="1",
            )
        )
        for record in found.get("ResourceRecordSets", []):
            if record["Name"] == wanted and record["Type"] == "A":
                return record
        return None

    def _change(self, action: str, record: dict) -> None:
        batch = {
            "Comment": "managed by anyship adapter",
            "Changes": [{"Action": action, "ResourceRecordSet": record}],
        }
        change = self._call(
            lambda api: api.change_resource_record_sets(
                HostedZoneId=self._zone_id(), ChangeBatch=batch
            )
        )
        self._call(
            lambda api: api.get_waiter("resource_record_sets_changed").wait(
                Id=change["ChangeInfo"]["Id"], WaiterConfig={"Delay": 5, "MaxAttempts": 24}
            )
        )

    def _call(self, call):
        try:
            return call(self._api())
        except DnsError:
            raise
        except Exception as exc:  # botocore의 ClientError, 시간 초과, 자격 증명 없음 등
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
            denied = code in ("AccessDenied", "AccessDeniedException")
            raise _fail(
                "dns_change_failed",
                "DNS 레코드를 바꾸지 못했습니다.",
                hint=(
                    "서비스 서버 역할의 Route 53 권한을 확인해 주세요."
                    if denied
                    else "잠시 후 다시 시도해 주세요."
                ),
                retryable=not denied,
            ) from None

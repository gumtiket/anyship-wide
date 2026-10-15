"""서비스 서버 역할의 Route 53 권한이 의도대로 좁혀졌는지 실제로 확인한다(자동 테스트가 아니다).

`infra/service-account`를 apply한 뒤 서비스 서버에서 실행한다.

    python scripts/check_dns_policy.py

허용되어야 할 요청(환경 와일드카드 A 레코드: `*.<환경ID>.onprem.<도메인>`과 `*.<환경ID>.aws.<도메인>`)은 통과하고,
그 밖의 이름과 종류는 AccessDenied여야 한다.
값은 문서용 시험 대역(192.0.2.1)만 쓰고, 거부되어야 할 요청이 통과해 버리면 바로 지운다.
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

results: list[tuple[str, bool]] = []
TEST_IP = "192.0.2.1"  # 문서용 예약 대역. 실제 서버가 아니다.


def expect(label: str, condition: bool) -> None:
    results.append((label, bool(condition)))
    print(f"      {'OK  ' if condition else 'FAIL'} {label}")


def record(name: str, kind: str = "A") -> dict:
    return {
        "Name": name,
        "Type": kind,
        "TTL": 60,
        "ResourceRecords": [{"Value": TEST_IP if kind == "A" else '"anyship-policy-test"'}],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="anyship.cloud")
    parser.add_argument(
        "--env-id", default="policytest", help="시험용 환경 ID(기존 환경 ID는 쓰지 마세요)"
    )
    args = parser.parse_args()
    domain, client = args.domain, boto3.client("route53")
    zone = client.list_hosted_zones_by_name(DNSName=f"{domain}.", MaxItems="1")["HostedZones"][0][
        "Id"
    ]
    created: list[dict] = []  # 만들어졌다면 반드시 지워야 하는 레코드

    def change(action: str, rec: dict) -> str:
        """'ok' 또는 AWS 오류 코드를 돌려준다."""
        try:
            client.change_resource_record_sets(
                HostedZoneId=zone,
                ChangeBatch={"Changes": [{"Action": action, "ResourceRecordSet": rec}]},
            )
            return "ok"
        except ClientError as error:
            return error.response["Error"]["Code"]

    scopes = ("onprem", "aws")  # infra/service-account/dns.tf가 허용하는 이름 범위와 같아야 한다
    allowed = [record(f"*.{args.env_id}.{scope}.{domain}") for scope in scopes]
    must_be_denied = [
        ("범위 없는 와일드카드(*.x.domain)", record(f"*.{args.env_id}.{domain}")),
        ("허용하지 않은 범위(*.x.gcp.domain)", record(f"*.{args.env_id}.gcp.{domain}")),
        ("루트에 가까운 이름(policytest.domain)", record(f"{args.env_id}.{domain}")),
    ]
    for scope in scopes:
        must_be_denied += [
            (
                f"와일드카드가 아닌 이름(www.env.{scope})",
                record(f"www.{args.env_id}.{scope}.{domain}"),
            ),
            (f"같은 이름의 TXT 종류({scope})", record(f"*.{args.env_id}.{scope}.{domain}", "TXT")),
        ]
    try:
        print("1) 허용되어야 할 요청: 환경 와일드카드 A 레코드(범위마다)")
        for rec in allowed:
            outcome = change("UPSERT", rec)
            if outcome == "ok":
                created.append(rec)
            expect(f"{rec['Name']} 만들기 ({outcome})", outcome == "ok")
            if outcome == "AccessDenied":
                print(
                    "      정책이 너무 좁습니다. 정규화된 이름 패턴(끝의 점 등)과 이 범위가 허용 목록에 있는지 확인하세요."
                )

        print("2) 거부되어야 할 요청")
        for label, rec in must_be_denied:
            outcome = change("UPSERT", rec)
            if outcome == "ok":
                created.append(rec)
            expect(f"{label}: {outcome}", outcome == "AccessDenied")
    finally:
        print("3) 정리: 만들어진 시험 레코드 삭제")
        for rec in created:
            outcome = change("DELETE", rec)
            expect(f"삭제 {rec['Type']} {rec['Name']} ({outcome})", outcome == "ok")

    failed = [label for label, ok in results if not ok]
    print(f"\n결과: {len(results) - len(failed)}/{len(results)} OK")
    for label in failed:
        print("  FAIL:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

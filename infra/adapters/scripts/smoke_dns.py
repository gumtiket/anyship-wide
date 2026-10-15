"""실제 Route 53에서 WildcardRecords를 시험하는 수동 시험(자동 테스트가 아니다).

서비스 서버에서 실행한다(인스턴스 역할이 Route 53 권한을 갖고 있다).

    pip install -e ".[aws]"
    python scripts/smoke_dns.py --ip 3.38.88.141                 # 온프레미스 범위(*.<환경ID>.onprem.<도메인>)
    python scripts/smoke_dns.py --ip 3.38.88.141 --scope aws     # AWS 범위(*.<환경ID>.aws.<도메인>)

`--existing-env`는 이미 레코드가 있는 환경을 **같은 값으로 ensure해서** 이름 조회가 맞는지 보는 용도다. 그 레코드의 TTL이
60초가 아니면 ensure가 레코드를 바꾸므로, 사람이 직접 만든 레코드(예: 손으로 관리하는 `*.test.aws.<도메인>`)는 지정하지 않는다.
기존 레코드는 건드리지 않는다. 시험용 환경 ID(기본 dnstest)의 레코드만 만들고 지우며,
시작과 끝에 영역의 전체 레코드 목록을 비교해서 다른 레코드가 그대로인지 확인한다.
"""

import argparse
import socket
import sys
import time

import boto3

from anyship_adapters.dns import SCOPES, DnsError, WildcardRecords

results: list[tuple[str, bool]] = []


def expect(label: str, condition: bool) -> None:
    results.append((label, bool(condition)))
    print(f"      {'OK  ' if condition else 'FAIL'} {label}")


def timed(call):
    started = time.perf_counter()
    value = call()
    print(f"      걸린 시간: {time.perf_counter() - started:.1f}초")
    return value


def snapshot(domain: str) -> set[tuple[str, str, str]]:
    """영역의 모든 레코드를 (이름, 종류, 값)의 집합으로 돌려준다."""
    client = boto3.client("route53")
    zone = client.list_hosted_zones_by_name(DNSName=f"{domain}.", MaxItems="1")["HostedZones"][0][
        "Id"
    ]
    found = set()
    for page in client.get_paginator("list_resource_record_sets").paginate(HostedZoneId=zone):
        for record in page["ResourceRecordSets"]:
            values = [r["Value"] for r in record.get("ResourceRecords", [])] or [
                str(record.get("AliasTarget"))
            ]
            found.add((record["Name"], record["Type"], ",".join(sorted(values))))
    return found


def resolves_to(host: str, ip: str, seconds: int = 90) -> float | None:
    """host가 ip로 풀릴 때까지 걸린 시간(초). 시간 안에 안 풀리면 None."""
    started = time.perf_counter()
    while time.perf_counter() - started < seconds:
        try:
            if ip in {info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)}:
                return time.perf_counter() - started
        except socket.gaierror:
            pass
        time.sleep(3)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ip", required=True, help="시험 레코드가 가리킬 공인 IP(온프레미스 서버의 IP)"
    )
    parser.add_argument(
        "--env-id", default="dnstest", help="시험용 환경 ID(기존 환경 ID는 쓰지 마세요)"
    )
    parser.add_argument(
        "--existing-env", default="demo", help="이미 레코드가 있는 환경(읽기만 확인)"
    )
    parser.add_argument("--domain", default="anyship.cloud")
    parser.add_argument(
        "--scope",
        choices=SCOPES,
        default="onprem",
        help="레코드 이름의 범위: onprem(*.<환경ID>.onprem.<도메인>) 또는 aws(*.<환경ID>.aws.<도메인>)",
    )
    args = parser.parse_args()
    if args.env_id == args.existing_env:
        print("시험용 환경 ID가 기존 환경과 같습니다. 다른 값을 쓰세요.")
        return 2

    records = WildcardRecords(base_domain=args.domain, scope=args.scope)
    host = f"probe.{args.env_id}.{args.scope}.{args.domain}"
    try:
        print("0) 시작 전 상태")
        before = snapshot(args.domain)
        print(f"      영역의 레코드 {len(before)}개를 기록했습니다.")
        leftover = records.remove(args.env_id)
        if leftover:
            print(f"      이전 시험이 남긴 {args.env_id} 레코드를 지웠습니다.")
            before = snapshot(args.domain)

        print(f"1) 기존 환경({args.existing_env})의 와일드카드 이름 조회 확인 (쓰기 없음)")
        existing = [
            r
            for r in before
            if r[0] == f"\\052.{args.existing_env}.{args.scope}.{args.domain}." and r[1] == "A"
        ]
        if existing:
            current_ip = existing[0][2]
            changed = timed(lambda: records.ensure(args.existing_env, current_ip))
            expect(
                "이미 있는 레코드를 같은 값으로 ensure하면 변경이 없다(\\052 이름 조회가 맞다)",
                changed is False,
            )
        else:
            print(f"      {args.existing_env} 레코드가 없어 이 단계를 건너뜁니다.")

        print(f"2) {args.env_id} 레코드 만들기 ({records.name_for(args.env_id)} -> {args.ip})")
        changed = timed(lambda: records.ensure(args.env_id, args.ip))
        expect("새 레코드가 만들어졌다", changed is True)
        after_create = snapshot(args.domain)
        expect("레코드가 영역에 하나 늘었다", len(after_create) == len(before) + 1)
        expect("다른 레코드는 그대로다", before <= after_create)

        print(f"3) 실제 DNS 조회: {host}")
        elapsed = resolves_to(host, args.ip)
        expect(f"와일드카드 아래의 임의 이름이 {args.ip}로 풀린다", elapsed is not None)
        if elapsed is not None:
            print(f"      풀리기까지 {elapsed:.1f}초")

        print("4) 같은 값으로 다시 ensure (멱등)")
        expect("변경이 없다", records.ensure(args.env_id, args.ip) is False)
    except DnsError as error:
        expect(f"DNS 오류 없이 진행된다 ({error.error.code}: {error.error.message})", False)
    finally:
        print("5) 정리: 시험 레코드 삭제")
        try:
            removed = timed(lambda: records.remove(args.env_id))
            expect("시험 레코드가 지워졌다", removed is True)
            expect("다시 지워도 안전하다(없으면 False)", records.remove(args.env_id) is False)
            expect("끝난 뒤 영역이 시작 전과 똑같다", snapshot(args.domain) == before)
        except DnsError as error:
            expect(f"정리 중 오류가 없다 ({error.error.code})", False)
            print(f"      직접 지워야 합니다: {records.name_for(args.env_id)}")

    failed = [label for label, ok in results if not ok]
    print(f"\n결과: {len(results) - len(failed)}/{len(results)} OK")
    for label in failed:
        print("  FAIL:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

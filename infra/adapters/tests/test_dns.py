import sys

import pytest

boto3 = pytest.importorskip(
    "boto3"
)  # DNS 기능은 선택 의존성이라, 없으면 이 파일의 테스트만 건너뛴다
from botocore.stub import Stubber  # noqa: E402

from anyship_adapters.dns import DnsError, WildcardRecords  # noqa: E402

ZONE = {
    "HostedZones": [
        {
            "Id": "/hostedzone/Z123",
            "Name": "anyship.cloud.",
            "CallerReference": "r",
            "Config": {"PrivateZone": False},
        }
    ],
    "DNSName": "anyship.cloud.",
    "IsTruncated": False,
    "MaxItems": "1",
}
NAME = "\\052.demo.onprem.anyship.cloud."  # Route 53은 와일드카드(*)를 \052로 돌려준다
RECORD = {"Name": NAME, "Type": "A", "TTL": 60, "ResourceRecords": [{"Value": "3.38.88.141"}]}
CHANGE = {
    "ChangeInfo": {"Id": "/change/C1", "Status": "PENDING", "SubmittedAt": "2026-10-09T00:00:00Z"}
}
INSYNC = {
    "ChangeInfo": {"Id": "/change/C1", "Status": "INSYNC", "SubmittedAt": "2026-10-09T00:00:00Z"}
}
FAKE_TOKEN = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    # 진짜 자격 증명이 없어도 클라이언트를 만들 수 있게 하고, 실수로 진짜 AWS를 부르는 일을 막는다.
    for key, value in {
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "ap-northeast-2",
    }.items():
        monkeypatch.setenv(key, value)


def listed(records):
    return {"ResourceRecordSets": records, "IsTruncated": False, "MaxItems": "1"}


def batch(action, record):
    return {
        "HostedZoneId": "Z123",
        "ChangeBatch": {
            "Comment": "managed by anyship adapter",
            "Changes": [{"Action": action, "ResourceRecordSet": record}],
        },
    }


def stubbed():
    client = boto3.client("route53")
    return client, Stubber(client)


def expect_lookup(stub, records, zone=True, name=NAME):
    if zone:
        stub.add_response(
            "list_hosted_zones_by_name", ZONE, {"DNSName": "anyship.cloud.", "MaxItems": "1"}
        )
    stub.add_response(
        "list_resource_record_sets",
        listed(records),
        {"HostedZoneId": "Z123", "StartRecordName": name, "StartRecordType": "A", "MaxItems": "1"},
    )


def expect_change(stub, expected=None):
    stub.add_response("change_resource_record_sets", CHANGE, expected)
    stub.add_response("get_change", INSYNC)


# --- 만들기와 갱신 -------------------------------------------------------------------------
def test_a_missing_record_is_created_with_an_upsert_of_the_wildcard_name():
    client, stub = stubbed()
    expect_lookup(stub, [])
    expect_change(
        stub,
        batch(
            "UPSERT",
            {
                "Name": "*.demo.onprem.anyship.cloud",
                "Type": "A",
                "TTL": 60,
                "ResourceRecords": [{"Value": "3.38.88.141"}],
            },
        ),
    )
    with stub:
        assert WildcardRecords(client).ensure("demo", "3.38.88.141") is True
        stub.assert_no_pending_responses()


def test_a_record_that_already_has_the_right_value_is_left_alone():
    client, stub = stubbed()
    expect_lookup(stub, [RECORD])
    with stub:  # 변경 응답을 준비하지 않았으므로, 변경을 시도하면 실패한다
        assert WildcardRecords(client).ensure("demo", "3.38.88.141") is False
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "current",
    [
        {**RECORD, "ResourceRecords": [{"Value": "52.78.1.1"}]},  # 서버의 IP가 바뀜
        {**RECORD, "TTL": 300},  # TTL이 다름
        {
            **RECORD,
            "ResourceRecords": [{"Value": "3.38.88.141"}, {"Value": "52.78.1.1"}],
        },  # 값이 둘
    ],
)
def test_a_record_that_differs_is_updated(current):
    client, stub = stubbed()
    expect_lookup(stub, [current])
    expect_change(stub)
    with stub:
        assert WildcardRecords(client).ensure("demo", "3.38.88.141") is True
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "found",
    [
        {**RECORD, "Name": "\\052.other.onprem.anyship.cloud."},  # 다른 환경의 레코드
        # 같은 이름의 다른 종류. 값과 TTL이 우리 레코드와 똑같아도 A 레코드가 아니면 현재 값으로 보면 안 된다.
        {**RECORD, "Type": "TXT"},
    ],
)
def test_only_this_environments_a_record_counts_as_the_current_one(found):
    client, stub = stubbed()
    expect_lookup(stub, [found])
    expect_change(stub)
    with stub:
        assert (
            WildcardRecords(client).ensure("demo", "3.38.88.141") is True
        )  # 못 찾았으니 새로 만든다


# --- 지우기 ----------------------------------------------------------------------------------
def test_remove_deletes_the_record_exactly_as_route_53_returned_it():
    client, stub = stubbed()
    expect_lookup(stub, [RECORD])
    expect_change(stub, batch("DELETE", RECORD))
    with stub:
        assert WildcardRecords(client).remove("demo") is True
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "found",
    [
        {**RECORD, "Name": "\\052.other.onprem.anyship.cloud."},  # 다른 환경의 레코드
        {**RECORD, "Type": "TXT"},  # 같은 이름의 A가 아닌 레코드
    ],
)
def test_remove_is_safe_to_repeat_and_never_touches_a_record_that_is_not_ours(found):
    client, stub = stubbed()
    expect_lookup(stub, [found])
    with stub:  # 삭제 응답을 준비하지 않았으므로, 삭제를 시도하면 실패한다
        assert WildcardRecords(client).remove("demo") is False
        stub.assert_no_pending_responses()


# --- 호스팅 영역 -----------------------------------------------------------------------------
def test_the_zone_is_looked_up_once_and_remembered():
    client, stub = stubbed()
    expect_lookup(stub, [RECORD])
    expect_lookup(stub, [RECORD], zone=False)  # 두 번째 호출에는 영역 조회 응답이 없다
    with stub:
        records = WildcardRecords(client)
        assert records.ensure("demo", "3.38.88.141") is False
        assert records.ensure("demo", "3.38.88.141") is False
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "zones",
    [
        [],
        [
            {
                "Id": "/hostedzone/Z9",
                "Name": "sub.anyship.cloud.",
                "CallerReference": "r",
                "Config": {"PrivateZone": False},
            }
        ],
        [
            {
                "Id": "/hostedzone/Z9",
                "Name": "anyship.cloud.",
                "CallerReference": "r",
                "Config": {"PrivateZone": True},
            }
        ],
    ],
)
def test_a_missing_wrong_or_private_zone_is_reported(zones):
    client, stub = stubbed()
    stub.add_response("list_hosted_zones_by_name", {**ZONE, "HostedZones": zones})
    with stub, pytest.raises(DnsError) as caught:
        WildcardRecords(client).ensure("demo", "3.38.88.141")
    assert caught.value.error.code == "dns_zone_not_found" and caught.value.error.hint


# --- 입력 검증: AWS를 부르기 전에 거부한다 ----------------------------------------------------------
class ExplodingClient:
    """어떤 메서드든 부르면 AssertionError를 낸다(AWS를 부르면 안 되는 상황을 확인하려고)."""

    def __getattr__(self, name):
        raise AssertionError(f"AWS를 호출하면 안 된다: {name}")


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.5",
        "192.168.1.10",
        "172.16.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "2001:db8::1",
        "::1",
        "not-an-ip",
        "",
        "3.38.88.141; x",
        "2606:4700:4700::1111",
    ],
)  # 마지막은 공인 IPv6라서 'IPv4만'이라는 규칙으로만 걸린다
def test_only_public_ipv4_addresses_are_accepted(ip):
    with pytest.raises(DnsError) as caught:
        WildcardRecords(ExplodingClient()).ensure("demo", ip)
    assert caught.value.error.code == "invalid_dns_input"


@pytest.mark.parametrize(
    "env_id", ["Demo", "1demo", "de.mo", "a", "", "demo.onprem", "*", "demo/x", "x" * 22]
)
def test_environment_ids_must_be_a_single_safe_dns_label(env_id):
    for call in (
        lambda: WildcardRecords(ExplodingClient()).ensure(env_id, "3.38.88.141"),
        lambda: WildcardRecords(ExplodingClient()).remove(env_id),
    ):
        with pytest.raises(DnsError) as caught:
            call()
        assert caught.value.error.code == "invalid_dns_input"


# --- 반영 대기와 오류 처리 -----------------------------------------------------------------------
def test_it_waits_for_the_change_to_be_in_sync_with_a_bounded_wait():
    seen = {}

    class Waiter:
        def wait(self, **kwargs):
            seen.update(kwargs)

    class Client:
        def list_hosted_zones_by_name(self, **kw):
            return ZONE

        def list_resource_record_sets(self, **kw):
            return listed([])

        def change_resource_record_sets(self, **kw):
            return CHANGE

        def get_waiter(self, name):
            seen["waiter"] = name
            return Waiter()

    WildcardRecords(Client()).ensure("demo", "3.38.88.141")
    assert seen["waiter"] == "resource_record_sets_changed" and seen["Id"] == "/change/C1"
    assert seen["WaiterConfig"] == {"Delay": 5, "MaxAttempts": 24}  # 최대 2분


def test_a_permission_error_says_what_to_check_and_is_not_worth_retrying():
    client, stub = stubbed()
    stub.add_client_error("list_hosted_zones_by_name", "AccessDenied")
    with stub, pytest.raises(DnsError) as caught:
        WildcardRecords(client).ensure("demo", "3.38.88.141")
    error = caught.value.error
    assert error.code == "dns_change_failed" and not error.retryable and "권한" in error.hint


def test_a_temporary_error_is_worth_retrying():
    client, stub = stubbed()
    stub.add_client_error("list_hosted_zones_by_name", "Throttling")
    with stub, pytest.raises(DnsError) as caught:
        WildcardRecords(client).ensure("demo", "3.38.88.141")
    assert caught.value.error.retryable


def test_the_error_never_repeats_what_aws_said():
    client, stub = stubbed()
    stub.add_client_error("list_hosted_zones_by_name", "InternalError", f"boom {FAKE_TOKEN}")
    with stub, pytest.raises(DnsError) as caught:
        WildcardRecords(client).ensure("demo", "3.38.88.141")
    assert FAKE_TOKEN not in caught.value.error.model_dump_json()


def test_a_missing_boto3_is_reported_as_unavailable_not_as_a_retryable_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)  # import boto3가 실패하게 만든다
    with pytest.raises(DnsError) as caught:
        WildcardRecords().ensure("demo", "3.38.88.141")
    error = caught.value.error
    assert (
        error.code == "dns_unavailable"
        and not error.retryable
        and "anyship-adapters[aws]" in error.hint
    )


# --- 이름 규칙 ------------------------------------------------------------------------------------
def test_the_record_name_is_one_wildcard_per_environment_under_the_configured_domain():
    assert WildcardRecords(ExplodingClient()).name_for("demo") == "*.demo.onprem.anyship.cloud"
    assert (
        WildcardRecords(ExplodingClient(), base_domain="example.org").name_for("team7")
        == "*.team7.onprem.example.org"
    )


# --- AWS 범위 ---------------------------------------------------------------------------------
AWS_NAME = "\\052.demo.aws.anyship.cloud."  # Route 53은 와일드카드(*)를 \052로 돌려준다
AWS_RECORD = {**RECORD, "Name": AWS_NAME}


def test_the_aws_scope_names_its_records_under_aws_and_the_default_stays_onprem():
    assert (
        WildcardRecords(ExplodingClient(), scope="aws").name_for("demo")
        == "*.demo.aws.anyship.cloud"
    )
    assert (
        WildcardRecords(ExplodingClient(), scope="aws", base_domain="example.org").name_for("team7")
        == "*.team7.aws.example.org"
    )
    assert WildcardRecords(ExplodingClient()).name_for("demo") == "*.demo.onprem.anyship.cloud"


def test_an_aws_record_is_created_with_an_upsert_under_aws():
    client, stub = stubbed()
    expect_lookup(stub, [], name=AWS_NAME)
    expect_change(
        stub,
        batch(
            "UPSERT",
            {
                "Name": "*.demo.aws.anyship.cloud",
                "Type": "A",
                "TTL": 60,
                "ResourceRecords": [{"Value": "3.38.88.141"}],
            },
        ),
    )
    with stub:
        assert WildcardRecords(client, scope="aws").ensure("demo", "3.38.88.141") is True
        stub.assert_no_pending_responses()


def test_an_aws_record_with_the_right_value_is_left_alone_and_a_new_ip_updates_it():
    client, stub = stubbed()
    expect_lookup(stub, [AWS_RECORD], name=AWS_NAME)
    with stub:
        assert WildcardRecords(client, scope="aws").ensure("demo", "3.38.88.141") is False
    client, stub = stubbed()
    expect_lookup(stub, [AWS_RECORD], name=AWS_NAME)
    expect_change(
        stub,
        batch(
            "UPSERT",
            {
                "Name": "*.demo.aws.anyship.cloud",
                "Type": "A",
                "TTL": 60,
                "ResourceRecords": [{"Value": "52.78.1.1"}],
            },
        ),
    )
    with stub:
        assert WildcardRecords(client, scope="aws").ensure("demo", "52.78.1.1") is True


def test_the_other_scopes_record_for_the_same_environment_is_never_taken_for_ours():
    # aws 범위가 같은 환경 ID의 onprem 레코드를 현재 값으로 보면, 새 레코드를 만들지 않고 잘못 끝난다.
    client, stub = stubbed()
    expect_lookup(stub, [RECORD], name=AWS_NAME)
    expect_change(stub)
    with stub:
        assert WildcardRecords(client, scope="aws").ensure("demo", "3.38.88.141") is True
        stub.assert_no_pending_responses()
    client, stub = stubbed()
    expect_lookup(stub, [AWS_RECORD])
    expect_change(stub)
    with stub:
        assert (
            WildcardRecords(client).ensure("demo", "3.38.88.141") is True
        )  # onprem 범위는 aws 레코드를 무시한다


def test_removing_in_the_aws_scope_deletes_only_the_aws_record():
    client, stub = stubbed()
    expect_lookup(stub, [AWS_RECORD], name=AWS_NAME)
    expect_change(stub, batch("DELETE", AWS_RECORD))
    with stub:
        assert WildcardRecords(client, scope="aws").remove("demo") is True
    client, stub = stubbed()
    expect_lookup(
        stub, [RECORD], name=AWS_NAME
    )  # 같은 환경의 onprem 레코드만 있으면 지울 것이 없다
    with stub:
        assert WildcardRecords(client, scope="aws").remove("demo") is False


@pytest.mark.parametrize("scope", ["", "ONPREM", "onprem.x", "gcp", "aws ", "*", "a/b"])
def test_an_unknown_scope_is_refused_before_anything_else(scope):
    with pytest.raises(ValueError):
        WildcardRecords(ExplodingClient(), scope=scope)

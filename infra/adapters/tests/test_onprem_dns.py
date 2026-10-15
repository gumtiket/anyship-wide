import pytest
from fakes import Log
from specs import GENERATED
from test_onprem import ENV, adapter, deploy, server

from anyship_adapters.dns import DnsError
from anyship_adapters.models import AdapterError

FAKE_TOKEN = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


class FakeRecords:
    """Route 53 대신 호출을 기록하는 가짜. error를 주면 그 오류로 실패한다."""

    def __init__(self, changed=True, error=None):
        self.calls, self._changed, self._error = [], changed, error

    def ensure(self, env_id, ip):
        self.calls.append(("ensure", env_id, ip))
        if self._error:
            raise DnsError(self._error)
        return self._changed

    def remove(self, env_id):
        self.calls.append(("remove", env_id))
        if self._error:
            raise DnsError(self._error)
        return self._changed


DNS_ERROR = AdapterError(
    code="dns_change_failed",
    message="DNS 레코드를 바꾸지 못했습니다.",
    hint="서비스 서버 역할의 Route 53 권한을 확인해 주세요.",
)


def commands(srv):
    return [" ".join(c) for c in srv.commands]


# --- deploy ---------------------------------------------------------------------------------
def test_deploy_points_the_environments_record_at_the_servers_public_ip():
    records, srv = FakeRecords(), server()
    result = deploy(srv, dns=records)
    assert result.ok and records.calls == [("ensure", "demo", "3.38.88.141")]


def test_the_public_ip_comes_from_the_server_not_from_the_environment_host():
    # 사용자가 입력한 host(도메인이나 사설 IP일 수도 있다) 대신, 서버가 밖에서 보이는 IP를 등록해야 한다.
    records = FakeRecords()
    deploy(server(**{"curl": (0, b"52.78.10.20\n", b"")}), dns=records)
    assert records.calls == [("ensure", "demo", "52.78.10.20")]


def test_without_dns_the_deploy_never_touches_records_and_says_it_skipped():
    log = Log()
    assert deploy(server(), log=log).ok
    assert "건너뜁니다" in log.text()
    assert log.steps()[1][:3] == (2, 7, "DNS 준비")


@pytest.mark.parametrize("changed, message", [(True, "새로 반영"), (False, "이미 맞습니다")])
def test_the_log_says_whether_the_record_was_changed(changed, message):
    log = Log()
    deploy(server(), log=log, dns=FakeRecords(changed=changed))
    assert message in log.text()


def test_dns_is_prepared_after_the_server_is_checked_and_before_the_image_is_sent():
    records, srv = FakeRecords(), server()
    order = []
    original = records.ensure
    records.ensure = lambda *a: order.append(len(srv.commands)) or original(*a)
    deploy(srv, dns=records)
    flat = commands(srv)
    sent_at = next(i for i, c in enumerate(flat) if "docker load" in c)
    assert flat[0].startswith("true") and 0 < order[0] <= sent_at


def test_a_dns_failure_stops_the_deploy_before_anything_is_sent_to_the_server():
    records, srv, log = FakeRecords(error=DNS_ERROR), server(), Log()
    result = deploy(srv, log=log, dns=records)
    assert not result.ok and result.error.code == "dns_change_failed" and result.error.hint
    assert not any("docker load" in c or "mkdir" in c for c in commands(srv))
    assert log.steps()[-1][0] == 2


def test_an_unknown_public_ip_stops_the_deploy_without_calling_dns():
    records = FakeRecords()
    result = deploy(server(**{"curl": (0, b"10.0.0.5", b"")}), dns=records)
    assert (
        result.error.code == "public_ip_unknown" and result.error.retryable and records.calls == []
    )


def test_an_unreachable_server_is_reported_before_dns_is_touched():
    records = FakeRecords()
    result = deploy(server(**{"true": (255, b"", b"no route")}), dns=records)
    assert result.error.code == "ssh_unreachable" and records.calls == []


def test_an_invalid_input_is_rejected_before_dns_is_touched():
    records = FakeRecords()
    result = adapter(server(), dns=records).deploy(
        ENV, GENERATED, "NOT A TAG", {}, Log(), set_name="onprem"
    )
    assert not result.ok and records.calls == []


def test_a_secret_in_a_dns_error_never_reaches_the_log_or_the_result():
    error = AdapterError(code="dns_change_failed", message=f"실패 {FAKE_TOKEN}")
    log = Log()
    result = deploy(server(), secrets={"TOKEN": FAKE_TOKEN}, log=log, dns=FakeRecords(error=error))
    assert FAKE_TOKEN not in log.text() and FAKE_TOKEN not in result.model_dump_json()


# --- 환경 DNS 삭제 ---------------------------------------------------------------------------------
def test_removing_the_environment_deletes_its_record_without_connecting_to_the_server():
    records, srv, log = FakeRecords(), server(), Log()
    result = adapter(srv, dns=records).remove_environment_dns(ENV, log)
    assert result.ok and records.calls == [("remove", "demo")] and srv.commands == []
    assert "지웠습니다" in log.text()


def test_removing_a_record_that_is_already_gone_succeeds():
    log = Log()
    assert adapter(server(), dns=FakeRecords(changed=False)).remove_environment_dns(ENV, log).ok
    assert "지울 DNS 레코드가 없습니다" in log.text()


def test_removing_without_dns_does_nothing_and_succeeds():
    log = Log()
    assert adapter(server()).remove_environment_dns(ENV, log).ok and "건너뜁니다" in log.text()


def test_a_removal_failure_is_returned_as_an_error():
    result = adapter(server(), dns=FakeRecords(error=DNS_ERROR)).remove_environment_dns(ENV, Log())
    assert not result.ok and result.error.code == "dns_change_failed"


def test_a_secret_in_a_removal_error_never_reaches_the_log_or_the_result():
    error, log = AdapterError(code="dns_change_failed", message=f"실패 {FAKE_TOKEN}"), Log()
    result = adapter(server(), dns=FakeRecords(error=error)).remove_environment_dns(ENV, log)
    assert (
        not result.ok
        and FAKE_TOKEN not in log.text()
        and FAKE_TOKEN not in result.model_dump_json()
    )


def test_destroying_one_app_never_removes_the_environments_record():
    # 레코드는 환경의 모든 앱이 함께 쓴다. 앱 하나를 지울 때 지우면 같은 환경의 다른 앱이 접속되지 않는다.
    records = FakeRecords()
    adapter(server(), dns=records).destroy(ENV, "todo", Log())
    assert records.calls == []

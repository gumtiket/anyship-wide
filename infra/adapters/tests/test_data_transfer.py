"""DB 이전 시험. 실제 SSH, Docker, PostgreSQL은 쓰지 않는다(가짜 서버). 실서버 확인은 따로 해야 한다."""

import shlex
import subprocess
from pathlib import Path

import pytest
from fakes import FakePopen, Log

from anyship_adapters import AwsEnvironment, OnpremEnvironment
from anyship_adapters import deployer as deployer_module
from anyship_adapters.aws_always_on import AwsAlwaysOnAdapter
from anyship_adapters.compose_host import ComposeHost
from anyship_adapters.data_transfer import (
    COUNT_SQL,
    SIZE_SQL,
    ComposeDbEndpoint,
    RdsDbEndpoint,
    inspect_source,
    transfer_database,
)
from anyship_adapters.deployer import Deployer
from anyship_adapters.models import AdapterError, TransferResult
from anyship_adapters.onprem import OnpremAdapter
from anyship_adapters.ssh import SshConnection, SshRunner

APP = "todo"
DIR = "/opt/apps/todo"
DUMP = b"PGDMP-custom-format-bytes" * 100
COUNTS = "notes|12\nusers|3\n"
PASSWORD = "Zq8" + "Vn2Lk9Xw4Pe7Rt5Yu1"  # 비밀 스캐너 오탐을 피하려고 조각으로 만든 가짜 비밀번호


class DbServer:
    """한 서버를 흉내 낸다: 웹·DB 컨테이너 상태, psql 질의에 대한 답, 복원으로 받은 바이트를 기억한다."""

    def __init__(
        self,
        *,
        web=True,
        exists=True,
        size=2048,
        counts=COUNTS,
        restore_code=0,
        restore_err=b"",
        dump_code=0,
        dump_err=b"",
        unreachable=False,
        restore_hang=False,
        stop_code=0,
    ):
        self.web, self.exists, self.size, self.counts = web, exists, size, counts
        self.restore_code, self.restore_err, self.restore_hang = (
            restore_code,
            restore_err,
            restore_hang,
        )
        self.dump_code, self.dump_err, self.unreachable, self.stop_code = (
            dump_code,
            dump_err,
            unreachable,
            stop_code,
        )
        self.commands: list[list[str]] = []
        self.restored: bytes | None = None
        self.popens: list[FakePopen] = []

    def _done(self, cmd, code=0, out=b"", err=b""):
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=err)

    def popen(self, cmd, **kwargs):
        remote = shlex.split(cmd[-1])
        self.commands.append(remote)
        fake = FakePopen(cmd, code=self.dump_code, image=DUMP, errors=self.dump_err)
        self.popens.append(fake)
        return fake

    def __call__(self, cmd, **kwargs):
        remote = shlex.split(cmd[-1])
        self.commands.append(remote)
        if self.unreachable:
            return self._done(cmd, 255, b"", b"offline")
        joined = " ".join(remote)
        if remote == ["true"]:
            return self._done(cmd)
        if remote[:2] == ["test", "-f"]:
            return self._done(cmd, 0 if self.exists else 1)
        if remote[:2] == ["docker", "compose"] and "ps" in remote:
            return self._done(cmd, 0, b"web\ndb\n" if self.web else b"db\n")
        if remote[:2] == ["docker", "compose"] and remote[-2:] == ["stop", "web"]:
            if self.stop_code == 0:
                self.web = False
            return self._done(cmd, self.stop_code, b"", b"cannot stop" if self.stop_code else b"")
        if remote[:2] == ["docker", "compose"] and "up" in remote:
            self.web = True
            return self._done(cmd)
        if "pg_restore" in joined:
            if self.restore_hang:
                raise subprocess.TimeoutExpired(cmd, 1)
            self.restored = kwargs["stdin"].read()
            return self._done(cmd, self.restore_code, b"", self.restore_err)
        if "psql" in joined:
            sql = kwargs["input"].decode()
            if sql == SIZE_SQL:
                return self._done(cmd, 0, f"{self.size}\n".encode())
            if sql == COUNT_SQL:
                return self._done(cmd, 0, self.counts.encode())
            return self._done(cmd, 0, b"1\n")
        return self._done(cmd)

    def ups(self):
        return [c for c in self.commands if c[:2] == ["docker", "compose"] and "up" in c]

    def stops(self):
        return [
            c for c in self.commands if c[:2] == ["docker", "compose"] and c[-2:] == ["stop", "web"]
        ]


def ssh_for(server):
    return SshRunner(SshConnection("3.38.88.141", Path("/key")), runner=server, popen=server.popen)


def compose_endpoint(server):
    ssh = ssh_for(server)
    return ComposeDbEndpoint(ssh, ComposeHost(ssh), APP)


def rds_endpoint(server):
    ssh = ssh_for(server)
    return RdsDbEndpoint(ssh, ComposeHost(ssh), APP)


def move(source=None, target=None, **kwargs):
    source, target = source or DbServer(), target or DbServer()
    log = Log()
    result = transfer_database(compose_endpoint(source), rds_endpoint(target), log, **kwargs)
    return result, source, target, log


# --- 성공 -----------------------------------------------------------------------------------
def test_the_dump_of_the_source_reaches_the_restore_of_the_target_byte_for_byte():
    result, source, target, _ = move()
    assert result.ok and target.restored == DUMP
    assert result.details == {"bytes": 2048, "tables": 2, "rows": 15, "source_stopped": True}


def test_writes_are_stopped_before_copying_and_the_target_app_starts_again_afterwards():
    result, source, target, _ = move()
    assert result.ok
    assert len(source.stops()) == 1 and len(target.stops()) == 1
    assert source.ups() == [] and not source.web  # 원본은 일부러 멈춘 채로 둔다
    assert len(target.ups()) == 1 and target.web


def test_the_steps_are_logged_in_order_and_the_database_stays_running():
    _, source, _, log = move()
    assert [name for _, _, name in log.steps()] == [
        "준비 확인",
        "쓰기 중지",
        "데이터 복사",
        "검증",
        "마무리",
    ]
    assert not any(c[-2:] == ["stop", "db"] for c in source.commands)  # DB 컨테이너는 멈추지 않는다


def test_a_source_that_was_not_running_is_not_stopped_and_reported_as_such():
    result, source, _, _ = move(source=DbServer(web=False))
    assert result.ok and source.stops() == [] and result.details["source_stopped"] is False


# --- 명령의 모양과 비밀 ------------------------------------------------------------------------
def test_the_onprem_database_is_reached_inside_its_container_without_a_password():
    _, source, target, _ = move()
    dump = next(c for c in source.commands if "pg_dump" in c)
    assert dump == [
        "docker",
        "compose",
        "--project-directory",
        DIR,
        "exec",
        "-T",
        "db",
        "pg_dump",
        "-U",
        "app",
        "-d",
        "app",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
    ]


def test_the_rds_side_reads_the_database_url_from_the_host_file_and_never_puts_it_on_a_command_line():
    _, _, target, _ = move()
    restore = next(c for c in target.commands if "pg_restore" in " ".join(c))
    script = restore[2]
    assert (
        '. "$env_file"' in script and "-e DATABASE_URL" in script and restore[4] == f"{DIR}/app.env"
    )
    everything = " ".join(" ".join(c) for c in target.commands)
    assert "postgresql://" not in everything and "PGPASSWORD" not in everything
    assert (
        "--single-transaction" in everything
        and "--no-owner" in everything
        and "--clean" in everything
    )


def test_queries_travel_on_standard_input_not_on_the_command_line():
    _, source, _, _ = move()
    assert not any(
        "count(*)" in " ".join(c) or "pg_database_size" in " ".join(c) for c in source.commands
    )


def test_errors_from_the_servers_never_leak_a_password_in_a_connection_url():
    leak = f"could not connect: postgresql://app_todo:{PASSWORD}@db.example.rds.amazonaws.com/app_todo".encode()
    result, _, _, log = move(target=DbServer(restore_code=1, restore_err=leak))
    assert not result.ok
    assert PASSWORD not in result.model_dump_json() and PASSWORD not in log.text()


# --- 실패와 되돌리기 -----------------------------------------------------------------------------
def test_a_failed_restore_restarts_the_source_and_leaves_the_target_untouched():
    result, source, target, _ = move(
        target=DbServer(restore_code=1, restore_err=b"relation already exists")
    )
    assert not result.ok and result.error.code == "restore_failed" and result.error.retryable
    assert len(source.ups()) == 1 and source.web  # 원본을 다시 켰다
    assert len(target.ups()) == 1 and target.web  # 멈췄던 대상도 다시 켰다
    assert result.details["stderr"] == "relation already exists"


def test_a_failed_dump_restarts_the_source_and_is_reported_as_a_dump_failure():
    result, source, target, _ = move(source=DbServer(dump_code=1, dump_err=b"connection refused"))
    assert not result.ok and result.error.code == "dump_failed"
    assert source.web and target.web


def test_a_restore_that_never_ends_stops_the_dump_and_restarts_the_apps():
    result, source, target, _ = move(target=DbServer(restore_hang=True))
    assert not result.ok and result.error.code == "restore_timeout"
    assert source.popens[0].killed and source.web and target.web


def test_a_row_count_mismatch_restarts_the_source_but_keeps_the_suspect_target_stopped():
    class Drift(DbServer):
        def __call__(self, cmd, **kwargs):
            if (
                "psql" in " ".join(shlex.split(cmd[-1]))
                and kwargs.get("input", b"").decode() == COUNT_SQL
                and self.restored is not None
            ):
                return self._done(cmd, 0, b"notes|11\nusers|3\n")
            return super().__call__(cmd, **kwargs)

    result, source, target, _ = move(target=Drift())
    assert (
        not result.ok
        and result.error.code == "verify_mismatch"
        and result.details["tables"] == ["notes"]
    )
    assert source.web and not target.web


def test_a_database_over_the_limit_is_refused_before_anything_is_stopped():
    result, source, target, _ = move(source=DbServer(size=5000), max_bytes=1000)
    assert not result.ok and result.error.code == "db_too_large"
    assert source.stops() == [] and target.stops() == [] and target.restored is None


def test_the_target_must_already_have_the_app():
    result, source, target, _ = move(target=DbServer(exists=False))
    assert not result.ok and result.error.code == "app_not_found" and "대상" in result.error.message
    assert source.stops() == []


@pytest.mark.parametrize("side", ["source", "target"])
def test_an_unreachable_server_stops_the_transfer_and_names_the_side(side):
    servers = {"source": DbServer(), "target": DbServer()}
    servers[side] = DbServer(unreachable=True)
    result, source, target, _ = move(source=servers["source"], target=servers["target"])
    assert not result.ok and result.error.code == "ssh_unreachable"
    assert ("원본" if side == "source" else "대상") in result.error.message
    assert source.stops() == [] and target.stops() == []


def test_if_stopping_the_second_app_fails_the_first_one_is_restarted():
    result, source, target, _ = move(target=DbServer(stop_code=1))
    assert not result.ok and result.error.code == "quiesce_failed"
    assert source.web and len(source.ups()) == 1


def test_an_unexpected_error_during_the_copy_still_restarts_what_was_stopped():
    source, target = DbServer(), DbServer()

    def boom(cmd, **kwargs):
        raise RuntimeError("disk exploded")

    source.popen = boom
    with pytest.raises(RuntimeError):
        transfer_database(compose_endpoint(source), rds_endpoint(target), Log())
    assert source.web and target.web


def test_a_bad_app_name_is_refused_when_the_endpoint_is_made():
    server = DbServer()
    with pytest.raises(ValueError):
        ComposeDbEndpoint(ssh_for(server), ComposeHost(ssh_for(server)), "Bad_App")


# --- Deployer 진입점 ----------------------------------------------------------------------------
ONPREM = OnpremEnvironment(env_id="demo", host="3.38.88.141")
AWS = AwsEnvironment(
    env_id="prod",
    role_arn="arn:aws:iam::123456789012:role/anyship",
    external_id="x" * 20,
    host="3.38.88.142",
)


def deployer(source_server, target_server):
    def onprem_connect(env):
        ssh = ssh_for(source_server)
        return ssh, ComposeHost(ssh)

    def aws_connect(env):
        ssh = ssh_for(target_server)
        return ssh, ComposeHost(ssh)

    adapters = {
        "onprem": OnpremAdapter(Path("/key"), connect=onprem_connect),
        "aws-always-on": AwsAlwaysOnAdapter(Path("/key"), connect=aws_connect),
    }
    return Deployer(adapters, builder=None)


def test_deployer_moves_data_from_an_onprem_environment_to_an_aws_environment():
    source, target = DbServer(), DbServer()
    log = Log()
    result = deployer(source, target).transfer_data(
        ONPREM, AWS, APP, log, source_set="onprem", target_set="aws-always-on"
    )
    assert result.ok and target.restored == DUMP
    assert {name for _, _, name in log.steps()} == {"데이터 이전"}  # 큰 단계 하나로 정리된다


def test_deployer_also_moves_data_the_other_way():
    onprem_server, aws_server = (
        DbServer(),
        DbServer(),
    )  # deployer()의 인자 순서: 온프레미스 서버, AWS 서버
    result = deployer(onprem_server, aws_server).transfer_data(
        AWS, ONPREM, APP, Log(), source_set="aws-always-on", target_set="onprem"
    )
    assert result.ok and onprem_server.restored == DUMP and aws_server.restored is None


def test_deployer_refuses_a_set_that_does_not_fit_the_environment_kind():
    result = deployer(DbServer(), DbServer()).transfer_data(
        ONPREM, AWS, APP, Log(), source_set="onprem", target_set="onprem"
    )
    assert not result.ok and result.error.code == "set_not_supported"
    result = deployer(DbServer(), DbServer()).transfer_data(
        AWS, AWS, APP, Log(), source_set="onprem", target_set="aws-always-on"
    )
    assert not result.ok and result.error.code == "set_not_supported"


def test_deployer_refuses_the_same_environment_and_bad_app_names():
    d = deployer(DbServer(), DbServer())
    same = d.transfer_data(ONPREM, ONPREM, APP, Log(), source_set="onprem", target_set="onprem")
    assert not same.ok and same.error.code == "same_environment"
    bad = d.transfer_data(
        ONPREM, AWS, "Bad_App", Log(), source_set="onprem", target_set="aws-always-on"
    )
    assert not bad.ok and bad.error.code == "invalid_spec"


def test_deployer_reports_a_missing_foundation_instead_of_a_pipeline_error():
    no_host = AwsEnvironment(
        env_id="prod", role_arn="arn:aws:iam::123456789012:role/anyship", external_id="x" * 20
    )
    result = deployer(DbServer(), DbServer()).transfer_data(
        ONPREM, no_host, APP, Log(), source_set="onprem", target_set="aws-always-on"
    )
    assert not result.ok and result.error.code == "foundation_missing"


def test_deployer_turns_an_unexpected_exception_into_a_generic_error_without_leaking_it():
    source, target = DbServer(), DbServer()

    def boom(cmd, **kwargs):
        raise RuntimeError(f"password={PASSWORD}")

    source.popen = boom
    log = Log()
    result = deployer(source, target).transfer_data(
        ONPREM, AWS, APP, log, source_set="onprem", target_set="aws-always-on"
    )
    assert not result.ok and result.error.code == "transfer_pipeline_error"
    assert PASSWORD not in result.model_dump_json() and PASSWORD not in log.text()
    assert source.web and target.web  # 멈췄던 앱은 되돌려졌다


def test_deployer_masks_secrets_again_on_whatever_the_transfer_returns(monkeypatch):
    # 모듈 안쪽의 마스킹이 새도 진입점에서 한 번 더 막는 이중 방어
    leaky = TransferResult(
        ok=False,
        error=AdapterError(
            code="restore_failed",
            message=f"failed: postgresql://app_todo:{PASSWORD}@db.example.com/app_todo",
        ),
    )
    monkeypatch.setattr(deployer_module, "transfer_database", lambda *args, **kwargs: leaky)
    result = deployer(DbServer(), DbServer()).transfer_data(
        ONPREM, AWS, APP, Log(), source_set="onprem", target_set="aws-always-on"
    )
    assert not result.ok and PASSWORD not in result.model_dump_json()


# --- 원본 미리 확인 --------------------------------------------------------------------------------
def test_inspecting_the_source_reports_the_size_and_changes_nothing():
    server = DbServer(size=4096)
    result = inspect_source(compose_endpoint(server), Log())
    assert result.ok and result.details == {"bytes": 4096}
    assert server.stops() == [] and server.ups() == [] and server.restored is None
    assert not any("pg_dump" in " ".join(c) for c in server.commands)  # 덤프도 뜨지 않는다


def test_inspecting_refuses_a_database_over_the_limit():
    result = inspect_source(compose_endpoint(DbServer(size=9000)), Log(), max_bytes=1000)
    assert not result.ok and result.error.code == "db_too_large" and result.details["bytes"] == 9000


def test_inspecting_reports_an_unreachable_server_a_missing_app_and_an_unusable_database():
    assert (
        inspect_source(compose_endpoint(DbServer(unreachable=True)), Log()).error.code
        == "ssh_unreachable"
    )
    assert (
        inspect_source(compose_endpoint(DbServer(exists=False)), Log()).error.code
        == "app_not_found"
    )

    class Broken(DbServer):
        def __call__(self, cmd, **kwargs):
            if "psql" in " ".join(shlex.split(cmd[-1])):
                return self._done(cmd, 2, b"", b"FATAL: the database system is starting up")
            return super().__call__(cmd, **kwargs)

    broken = inspect_source(compose_endpoint(Broken()), Log())
    assert not broken.ok and broken.error.code == "source_db_unavailable" and broken.error.retryable


def test_deployer_inspects_the_source_through_its_adapter():
    source = DbServer(size=777)
    log = Log()
    result = deployer(source, DbServer()).check_transfer_source(ONPREM, APP, log, set_name="onprem")
    assert result.ok and result.details == {"bytes": 777}
    assert {name for _, _, name in log.steps()} == {"원본 확인"}


def test_deployer_inspect_refuses_wrong_sets_bad_names_and_a_missing_foundation():
    d = deployer(DbServer(), DbServer())
    assert (
        d.check_transfer_source(ONPREM, APP, Log(), set_name="aws-always-on").error.code
        == "set_not_supported"
    )
    assert (
        d.check_transfer_source(ONPREM, "Bad_App", Log(), set_name="onprem").error.code
        == "invalid_spec"
    )
    no_host = AwsEnvironment(
        env_id="prod", role_arn="arn:aws:iam::123456789012:role/anyship", external_id="x" * 20
    )
    assert (
        d.check_transfer_source(no_host, APP, Log(), set_name="aws-always-on").error.code
        == "foundation_missing"
    )


def test_if_the_target_cannot_start_after_the_copy_the_source_serves_again():
    class WontStart(DbServer):
        def __call__(self, cmd, **kwargs):
            remote = shlex.split(cmd[-1])
            if remote[:2] == ["docker", "compose"] and "up" in remote:
                return self._done(cmd, 1, b"", b"port is already allocated")
            return super().__call__(cmd, **kwargs)

    result, source, target, _ = move(target=WontStart())
    assert not result.ok and result.error.code == "target_start_failed" and result.error.retryable
    assert source.web and len(source.ups()) == 1  # 원본을 다시 켰다
    assert result.details["stderr"] == "port is already allocated"

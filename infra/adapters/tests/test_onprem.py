import json
from pathlib import Path

import pytest
from fakes import FakePopen, FakeServer, Log
from specs import GENERATED, HAND_WRITTEN, make

from anyship_adapters import OnpremEnvironment
from anyship_adapters.compose_host import ComposeHost
from anyship_adapters.onprem import OnpremAdapter
from anyship_adapters.ssh import SshConnection, SshRunner

ENV = OnpremEnvironment(env_id="demo", host="3.38.88.141")
TAG = "3f2a9c1"
SECRET = "s3cr3t-value-123"
UP = ("docker", "compose", "--project-directory", "/opt/apps/todo", "up")
RUN = ("docker", "compose", "--project-directory", "/opt/apps/todo", "run")


def server(**overrides):
    """정상 서버: Traefik이 실행 중이고, 공개 IP를 알려 준다. overrides로 특정 명령만 바꾼다."""
    # 실제 서버에서 Compose로 띄운 컨테이너의 이름은 traefik이 아니라 traefik-traefik-1이다.
    responses = {
        ("docker", "ps"): (0, b"traefik-traefik-1\n", b""),
        ("curl",): (0, b"3.38.88.141\n", b""),
    }
    responses.update({tuple(key.split()): value for key, value in overrides.items()})
    return FakeServer(responses=responses)


def adapter(srv, healthy=lambda url, verify_tls: (True, 200), **kw):
    ssh = SshRunner(SshConnection("3.38.88.141", Path("/key")), runner=srv)
    host = ComposeHost(ssh, popen=lambda cmd, **k: FakePopen(cmd, **k))
    return OnpremAdapter(Path("/key"), connect=lambda env: (ssh, host), healthy=healthy, **kw)


def deploy(srv, spec=GENERATED, secrets=None, log=None, **kw):
    return adapter(srv, **kw).deploy(ENV, spec, TAG, secrets or {}, log or Log(), set_name="onprem")


# --- check ------------------------------------------------------------------------------------
def test_check_passes_and_reports_the_public_ip_in_four_numbered_steps():
    log = Log()
    result = adapter(server()).check(ENV, log)
    assert result.ok and result.details == {"public_ip": "3.38.88.141"}
    assert [s[:2] for s in log.steps()] == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_check_reports_an_unreachable_server_and_stops_there():
    srv = server(**{"true": (255, b"", b"no route")})
    result = adapter(srv).check(ENV, Log())
    assert result.error.code == "ssh_unreachable" and result.error.retryable
    assert [c[0] for c in srv.commands] == ["true"]


def test_the_proxy_is_found_by_its_compose_labels_not_by_a_container_name():
    srv = server()
    adapter(srv).check(ENV, Log())
    command = next(c for c in srv.commands if c[:2] == ["docker", "ps"])
    assert "label=com.docker.compose.project=traefik" in command
    assert "label=com.docker.compose.service=traefik" in command
    assert not any(part.startswith("name=") for part in command)


def test_check_tells_a_failing_command_from_an_unreachable_server():
    assert (
        adapter(server(**{"true": (1, b"", b"")})).check(ENV, Log()).error.code
        == "ssh_command_failed"
    )


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"docker compose version": (1, b"", b"")}, "docker_missing"),
        ({"docker ps": (0, b"", b"")}, "proxy_not_ready"),  # 실행 중인 Traefik이 없다
        ({"curl": (0, b"10.0.0.5", b"")}, "public_ip_unknown"),  # 사설 주소
        ({"curl": (0, b"169.254.169.254", b"")}, "public_ip_unknown"),
        ({"curl": (0, b"<html>", b"")}, "public_ip_unknown"),
        ({"curl": (0, b"2001:db8::1", b"")}, "public_ip_unknown"),  # IPv6는 지원하지 않는다
        ({"curl": (22, b"", b"")}, "public_ip_unknown"),
    ],
)
def test_check_failures_have_a_specific_code(overrides, code):
    result = adapter(server(**overrides)).check(ENV, Log())
    assert not result.ok and result.error.code == code and result.error.hint


# --- deploy: 성공 -----------------------------------------------------------------------------
def test_deploy_returns_the_public_url_after_seven_numbered_steps():
    log = Log()
    result = deploy(server(), log=log)
    assert (
        result.ok
        and result.url == "https://todo.demo.onprem.anyship.cloud"
        and result.image_tag == TAG
    )
    assert log.steps() == [
        (1, 7, "환경 점검"),
        (2, 7, "DNS 준비"),
        (3, 7, "이미지 전달"),
        (4, 7, "파일 쓰기"),
        (5, 7, "앱 시작"),
        (6, 7, "마이그레이션"),
        (7, 7, "헬스체크"),
    ]


def test_deploy_does_things_in_a_safe_order():
    srv = server()
    deploy(srv)
    flat = [" ".join(c) for c in srv.commands]

    def first(text):
        return next(i for i, command in enumerate(flat) if text in command)

    # 이미지 -> 디렉터리와 파일 -> 시작 -> 마이그레이션
    assert first("docker load") < first("mkdir -p") < first(" up -d") < first(" run --rm")


def test_deploy_without_a_release_step_skips_the_migration_but_keeps_the_numbering():
    log = Log()
    srv = server()
    result = deploy(srv, make(release=None), log=log)
    assert result.ok and (6, 7, "마이그레이션") in log.steps()
    assert not any(tuple(c[:5]) == RUN for c in srv.commands)


def test_the_address_follows_the_environment_and_the_domain():
    ssh_env = OnpremEnvironment(env_id="team7", host="3.38.88.141")
    srv = server()
    ssh = SshRunner(SshConnection("3.38.88.141", Path("/key")), runner=srv)
    host = ComposeHost(ssh, popen=lambda c, **k: FakePopen(c, **k))
    seen = []
    onprem = OnpremAdapter(
        Path("/key"),
        base_domain="example.org",
        connect=lambda e: (ssh, host),
        healthy=lambda url, verify_tls: seen.append((url, verify_tls)) or (True, 200),
        verify_tls=False,
    )
    result = onprem.deploy(ssh_env, GENERATED, TAG, {}, Log(), set_name="onprem")
    assert result.url == "https://todo.team7.onprem.example.org"
    assert seen == [("https://todo.team7.onprem.example.org/healthz", False)]


def test_a_second_deploy_reuses_the_secrets_the_first_one_created():
    srv = server()
    first = deploy(srv)
    key = srv.files["/opt/apps/todo/app.env"]
    second = deploy(srv)
    assert set(first.details["generated"]) == {"SECRET_KEY", "POSTGRES_PASSWORD"}
    assert second.details["generated"] == [] and srv.files["/opt/apps/todo/app.env"] == key


def test_warnings_are_logged_and_returned():
    log = Log()
    result = deploy(server(), spec=HAND_WRITTEN, log=log)
    assert any(e.level == "warn" and "object_storage" in e.message for e in log.events)
    assert any("object_storage" in w for w in result.details["warnings"])


# --- deploy: 실패 -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides, kw, code, last_step",
    [
        ({"true": (255, b"", b"")}, {}, "ssh_unreachable", 1),
        ({"docker load": (1, b"", b"no space")}, {}, "image_transfer_failed", 3),
        ({" ".join(UP): (1, b"", b"port in use")}, {}, "container_start_failed", 5),
        ({" ".join(RUN): (1, b"", b"relation missing")}, {}, "migration_failed", 6),
        ({}, {"healthy": lambda url, verify_tls: (False, 502)}, "healthcheck_failed", 7),
    ],
)
def test_each_failure_has_its_own_code_and_stops_the_deploy(overrides, kw, code, last_step):
    srv, log = server(**overrides), Log()
    result = deploy(srv, log=log, **kw)
    assert not result.ok and result.error.code == code and result.error.hint
    assert [s[0] for s in log.steps()][-1] == last_step
    assert log.events[-1].level == "error"


def test_a_failed_file_write_is_reported_and_nothing_is_started():
    srv = FakeServer(fail_path_suffix="compose.yaml", responses=server().responses)
    result = deploy(srv)
    assert result.error.code == "server_write_failed"
    assert not any(tuple(c[:5]) == UP for c in srv.commands)


def test_the_health_failure_message_includes_the_last_status():
    result = deploy(server(), healthy=lambda url, verify_tls: (False, 502))
    assert "502" in result.error.message


def test_bad_requests_are_rejected_before_touching_the_server():
    srv = server()
    assert deploy(srv, {"app": "Bad Name"}).error.code == "invalid_spec"
    assert (
        adapter(srv).deploy(ENV, GENERATED, TAG, {}, Log(), set_name="aws-always-on").error.code
        == "set_not_supported"
    )
    assert (
        adapter(srv).deploy(ENV, GENERATED, "latest", {}, Log(), set_name="onprem").error.code
        == "invalid_image_tag"
    )
    assert (
        deploy(srv, make(env=[{"name": "API_KEY", "secret": True}])).error.code == "missing_secret"
    )
    assert srv.commands == []  # 서버에 접속조차 하지 않았다


# --- 비밀 -------------------------------------------------------------------------------------
def test_user_secrets_never_reach_logs_results_or_the_compose_file():
    srv, log = server(), Log()
    spec = make(env=[{"name": "API_KEY", "secret": True}])
    result = deploy(srv, spec, {"API_KEY": SECRET}, log)
    assert result.ok and SECRET in srv.files["/opt/apps/todo/app.env"].decode()
    assert SECRET not in log.text() + result.model_dump_json()
    assert SECRET not in srv.files["/opt/apps/todo/compose.yaml"].decode()


def test_server_errors_are_shown_with_secrets_removed_including_secrets_read_from_the_server():
    srv, log = server(), Log()
    deploy(srv)  # 첫 배포가 서버에 SECRET_KEY를 만든다
    stored = srv.files["/opt/apps/todo/app.env"].decode()
    server_secret = next(
        line for line in stored.splitlines() if line.startswith("SECRET_KEY")
    ).split("'")[1]
    srv.responses[UP] = (1, b"", f"crash SECRET_KEY={server_secret}".encode())
    result = deploy(
        srv, make(env=[{"name": "SECRET_KEY", "secret": True, "generate": True}]), {}, log
    )
    leaked = log.text() + json.dumps(result.details) + result.model_dump_json()
    assert server_secret not in leaked and not result.ok
    assert "***" in result.details["stderr"]

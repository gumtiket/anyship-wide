from pathlib import Path

import pytest
from fakes import LifecycleServer, Log
from specs import GENERATED

from anyship_adapters import OnpremEnvironment
from anyship_adapters.compose_host import ComposeHost
from anyship_adapters.onprem import OnpremAdapter
from anyship_adapters.ssh import SshConnection, SshRunner

ENV = OnpremEnvironment(env_id="demo", host="3.38.88.141")
V1, V2 = "aaaaaaa", "bbbbbbb"
APP = "todo"
DIR = "/opt/apps/todo"
UP = ("docker", "compose", "--project-directory", DIR, "up")
DOWN = ("docker", "compose", "--project-directory", DIR, "down")
# 비밀 스캐너 오탐을 피하려고 조각으로 만든 가짜 토큰
FAKE_TOKEN = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


class Health:
    """공개 주소 확인을 흉내 낸다. 어떤 인자로 불렸는지 기록한다."""

    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return (self.ok, 200 if self.ok else 502)


def setup(health=None, **responses):
    srv = LifecycleServer(responses={tuple(k.split()): v for k, v in responses.items()})
    ssh = SshRunner(SshConnection("3.38.88.141", Path("/key")), runner=srv)
    check = health or Health()
    adapter = OnpremAdapter(
        Path("/key"), connect=lambda env: (ssh, ComposeHost(ssh, popen=srv.popen)), healthy=check
    )
    return adapter, srv, check


def deploy(adapter, tag):
    return adapter.deploy(ENV, GENERATED, tag, {}, Log(), set_name="onprem")


def deployed():
    adapter, srv, health = setup()
    assert deploy(adapter, V1).ok and deploy(adapter, V2).ok
    return adapter, srv, health


# --- status -----------------------------------------------------------------------------------
def test_status_of_an_app_that_was_never_deployed():
    adapter, srv, health = setup()
    result = adapter.status(ENV, APP)
    assert result.ok and result.state == "not_deployed" and result.url is None
    assert health.calls == []


def test_status_of_a_running_app_reports_the_tag_the_url_and_checks_health_once():
    adapter, srv, _ = deployed()
    health = Health()
    adapter._healthy = health
    result = adapter.status(ENV, APP)
    assert (result.ok, result.state, result.image_tag) == (True, "running", V2)
    assert result.url == "https://todo.demo.onprem.anyship.cloud"
    assert health.calls == [
        ("https://todo.demo.onprem.anyship.cloud/healthz", {"attempts": 1, "verify_tls": True})
    ]  # 한 번만 확인해서 빠르다


def test_status_tells_unhealthy_and_stopped_apart():
    adapter, srv, _ = deployed()
    adapter._healthy = Health(ok=False)
    assert adapter.status(ENV, APP).state == "unhealthy"
    srv.running = set()
    health = Health()
    adapter._healthy = health
    stopped = adapter.status(ENV, APP)
    assert stopped.state == "stopped" and stopped.image_tag == V2 and health.calls == []


def test_status_reports_an_unreachable_server_as_an_error_not_as_a_state():
    adapter, srv, _ = setup(**{"true": (255, b"", b"")})
    result = adapter.status(ENV, APP)
    assert not result.ok and result.error.code == "ssh_unreachable"


# --- rollback -----------------------------------------------------------------------------------
def test_rollback_runs_the_older_image_and_changes_nothing_else():
    adapter, srv, _ = deployed()
    secrets = {n: srv.files[f"{DIR}/{n}"] for n in (".env", "app.env")}
    old = srv.files[f"{DIR}/compose.yaml"].decode().splitlines()
    log = Log()
    result = adapter.rollback(ENV, APP, V1, log)
    new = srv.files[f"{DIR}/compose.yaml"].decode().splitlines()
    assert (
        result.ok
        and result.image_tag == V1
        and result.url == "https://todo.demo.onprem.anyship.cloud"
    )
    assert [(a, b) for a, b in zip(old, new, strict=False) if a != b] == [
        (f'    image: "todo:{V2}"', f'    image: "todo:{V1}"')
    ]
    assert {n: srv.files[f"{DIR}/{n}"] for n in secrets} == secrets  # 비밀 파일은 그대로
    assert [s[:2] for s in log.steps()] == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert adapter.status(ENV, APP).image_tag == V1


@pytest.mark.parametrize(
    "app, tag, code",
    [
        (APP, "ccccccc", "image_not_found"),  # 서버에 이 버전의 이미지가 없다
        ("other", V1, "app_not_found"),
        (APP, "latest", "invalid_image_tag"),
        ("Bad Name", V1, "invalid_spec"),
    ],
)
def test_rollback_that_cannot_proceed_changes_nothing_on_the_server(app, tag, code):
    adapter, srv, _ = deployed()
    before, ups = dict(srv.files), sum(1 for c in srv.commands if tuple(c[:5]) == UP)
    result = adapter.rollback(ENV, app, tag, Log())
    assert not result.ok and result.error.code == code and result.error.hint
    assert srv.files == before and sum(1 for c in srv.commands if tuple(c[:5]) == UP) == ups


def test_rollback_reports_start_and_health_failures_with_their_own_codes():
    adapter, srv, _ = deployed()
    srv.responses[UP] = (1, b"", b"port in use")
    assert adapter.rollback(ENV, APP, V1, Log()).error.code == "container_start_failed"
    del srv.responses[UP]
    adapter._healthy = Health(ok=False)
    failed = adapter.rollback(ENV, APP, V1, Log())
    assert failed.error.code == "healthcheck_failed" and "502" in failed.error.message


def test_rollback_on_an_unreachable_server_stops_at_the_first_step():
    adapter, srv, _ = setup(**{"true": (255, b"", b"")})
    log = Log()
    assert adapter.rollback(ENV, APP, V1, log).error.code == "ssh_unreachable"
    assert [s[0] for s in log.steps()] == [1] and log.events[-1].level == "error"


# --- destroy ------------------------------------------------------------------------------------
def test_destroy_removes_the_containers_the_data_and_the_directory():
    adapter, srv, _ = deployed()
    log = Log()
    result = adapter.destroy(ENV, APP, log)
    assert result.ok and srv.downed == [DIR] and srv.removed == [DIR]
    assert not any(path.startswith(DIR) for path in srv.files)
    assert [s[:2] for s in log.steps()] == [(1, 2), (2, 2)]
    assert adapter.status(ENV, APP).state == "not_deployed"


def test_destroy_is_safe_to_repeat_and_does_not_run_compose_for_an_app_that_is_gone():
    adapter, srv, _ = deployed()
    assert adapter.destroy(ENV, APP, Log()).ok
    srv.downed.clear()
    assert adapter.destroy(ENV, APP, Log()).ok
    assert srv.downed == []


def test_destroy_keeps_the_files_when_the_containers_could_not_be_removed_and_hides_secrets():
    adapter, srv, _ = deployed()
    srv.responses[DOWN] = (1, b"", f"cannot remove token={FAKE_TOKEN}".encode())
    result = adapter.destroy(ENV, APP, Log())
    assert not result.ok and result.error.code == "destroy_failed"
    assert FAKE_TOKEN not in result.model_dump_json() and "***" in result.details["stderr"]
    assert (
        srv.removed == [] and f"{DIR}/app.env" in srv.files
    )  # 컨테이너가 남았는데 파일만 지우지 않는다


@pytest.mark.parametrize("app", ["..", "../etc", "a/b", "Todo", "a b", ""])
def test_destroy_refuses_names_that_could_point_outside_the_apps_directory(app):
    adapter, srv, _ = setup()
    result = adapter.destroy(ENV, app, Log())
    assert result.error.code == "invalid_spec" and srv.commands == []


def test_destroy_on_an_unreachable_server_is_an_error():
    adapter, srv, _ = setup(**{"true": (255, b"", b"")})
    assert adapter.destroy(ENV, APP, Log()).error.code == "ssh_unreachable"


# --- 전체 수명 주기 ---------------------------------------------------------------------------------
def test_the_whole_life_of_an_app():
    adapter, srv, _ = setup()
    assert adapter.status(ENV, APP).state == "not_deployed"
    assert deploy(adapter, V1).ok and deploy(adapter, V2).ok
    assert (adapter.status(ENV, APP).state, adapter.status(ENV, APP).image_tag) == ("running", V2)
    assert adapter.rollback(ENV, APP, V1, Log()).ok and adapter.status(ENV, APP).image_tag == V1
    assert adapter.destroy(ENV, APP, Log()).ok and adapter.status(ENV, APP).state == "not_deployed"

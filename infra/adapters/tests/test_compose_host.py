import subprocess
import urllib.error
from pathlib import Path

import pytest
from fakes import FakePopen, FakeServer
from specs import GENERATED

from anyship_adapters import compose_host, render_stack
from anyship_adapters.compose_host import ComposeHost, wait_healthy
from anyship_adapters.ssh import SshConnection, SshRunner

HOST = "todo.demo.onprem.anyship.cloud"


def make(fail_path_suffix=None, popen=None):
    server = FakeServer(fail_path_suffix)
    ssh = SshRunner(SshConnection("3.38.88.141", Path("/key")), runner=server)
    kwargs = {"popen": popen} if popen else {}
    return ComposeHost(ssh, **kwargs), server


def stack(**kw):
    return render_stack(GENERATED, host=HOST, image_tag=kw.pop("image_tag", "3f2a9c1"), **kw)


# --- 파일 쓰기와 이전 값 읽기 ----------------------------------------------------------------
def test_write_stack_creates_the_directory_then_writes_secrets_files_before_the_compose_file():
    host, server = make()
    result = host.write_stack("todo", stack())
    assert result.ok
    assert server.commands[0] == ["mkdir", "-p", "/opt/apps/todo"]
    assert server.written() == [".env", "app.env", "compose.yaml"]
    modes = {Path(c[4]).name: c[5] for c in server.commands if c[:2] == ["sh", "-c"]}
    assert modes == {".env": "0600", "app.env": "0600", "compose.yaml": "0644"}


def test_write_stack_stops_at_the_first_failure():
    host, server = make(fail_path_suffix="app.env")
    result = host.write_stack("todo", stack())
    assert not result.ok and result.stderr == "disk full"
    assert ".env" in server.written() and "compose.yaml" not in server.written()


def test_the_files_on_the_server_hold_exactly_what_was_rendered():
    host, server = make()
    rendered = stack()
    host.write_stack("todo", rendered)
    assert server.files["/opt/apps/todo/compose.yaml"].decode() == rendered.compose_yaml
    assert server.files["/opt/apps/todo/app.env"].decode() == rendered.app_env


def test_first_deploy_has_no_previous_values():
    assert make()[0].read_previous_env("todo") == {}


def test_a_redeploy_reuses_the_generated_secrets_found_on_the_server():
    host, _ = make()
    first = stack()
    host.write_stack("todo", first)
    previous = host.read_previous_env("todo")
    assert {"SECRET_KEY", "POSTGRES_PASSWORD"} <= set(previous)
    second = stack(image_tag="bbbbbbb", previous_env=previous)
    assert second.generated == ()
    assert second.app_env == first.app_env and second.compose_env == first.compose_env


def test_lines_that_are_not_plain_quoted_assignments_are_ignored_when_reading():
    host, server = make()
    server.files["/opt/apps/todo/app.env"] = (
        b"GOOD='1'\nBAD=2\nlower='x'\nALSO_BAD='a'b'\n# comment\n"
    )
    assert host.read_previous_env("todo") == {"GOOD": "1"}


# --- 이미지 전달 -----------------------------------------------------------------------------
def test_the_image_is_streamed_from_docker_save_into_docker_load_on_the_server():
    saves = []
    host, server = make(popen=lambda cmd, **kw: saves.append(FakePopen(cmd, **kw)) or saves[-1])
    result = host.load_image("todo:3f2a9c1")
    assert result.ok
    assert saves[0].cmd == ["docker", "save", "todo:3f2a9c1"]
    assert server.last() == ["docker", "load"]
    assert "stdin" in server.kwargs[-1] and "input" not in server.kwargs[-1]
    assert set(saves[0].kwargs["env"]) <= {"PATH", "HOME", "LANG"}


def test_a_failing_docker_save_is_reported_even_if_the_remote_side_succeeded():
    host, _ = make(popen=lambda cmd, **kw: FakePopen(cmd, code=1, errors=b"No such image", **kw))
    result = host.load_image("todo:3f2a9c1")
    assert not result.ok and result.returncode == 1 and "No such image" in result.stderr


def test_a_timed_out_transfer_stops_docker_save_and_is_reported_as_a_timeout():
    saves = []

    def popen(cmd, **kw):
        saves.append(FakePopen(cmd, code=-9, **kw))  # 죽은 프로세스는 0이 아닌 코드로 끝난다
        return saves[-1]

    def hangs(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    host = ComposeHost(
        SshRunner(SshConnection("3.38.88.141", Path("/key")), runner=hangs), popen=popen
    )
    result = host.load_image("todo:3f2a9c1", timeout=1)
    assert saves[0].killed
    assert result.timed_out and not result.ok  # 종료 코드 -9가 아니라 시간 초과로 보인다


@pytest.mark.parametrize("image", ["todo:latest", "todo", "Todo:3f2a9c1", "todo:3f2a9c1; echo", ""])
def test_only_app_and_commit_sha_images_are_accepted(image):
    host, server = make()
    with pytest.raises(ValueError):
        host.load_image(image)
    assert server.commands == []


# --- compose 실행과 마이그레이션 ---------------------------------------------------------------
def test_up_runs_compose_in_the_app_directory():
    host, server = make()
    host.up("todo")
    assert server.last() == [
        "docker",
        "compose",
        "--project-directory",
        "/opt/apps/todo",
        "up",
        "-d",
        "--remove-orphans",
    ]


def test_migrate_runs_inside_the_app_container_and_the_command_stays_one_argument():
    host, server = make()
    host.migrate("todo", "python -m app.migrate; echo done")
    assert server.last()[-4:] == ["web", "sh", "-c", "python -m app.migrate; echo done"]
    assert server.last()[:6] == [
        "docker",
        "compose",
        "--project-directory",
        "/opt/apps/todo",
        "run",
        "--rm",
    ]


def test_the_migration_container_is_hidden_from_traefik():
    # 임시 컨테이너가 앱의 Traefik 라벨을 물려받으면, 사라질 때 Traefik이 없는 주소로 요청을 보내 502가 난다.
    host, server = make()
    host.migrate("todo", "python -m app.migrate")
    command = server.last()
    assert command[command.index("--label") + 1] == "traefik.enable=false"
    assert command.index("--label") < command.index(
        "web"
    )  # 서비스 이름보다 앞에 와야 run의 옵션으로 읽힌다
    assert command.count("--label") == 1 and command[-1] == "python -m app.migrate"


@pytest.mark.parametrize(
    "call",
    [
        lambda h: h.up("Bad Name"),
        lambda h: h.up("../etc"),
        lambda h: h.migrate("a", "x"),
        lambda h: h.write_stack("x" * 70, None),
        lambda h: h.read_previous_env("a;b"),
    ],
)
def test_bad_app_names_are_rejected_before_anything_runs(call):
    host, server = make()
    with pytest.raises(ValueError):
        call(host)
    assert server.commands == []


# --- 헬스체크 ----------------------------------------------------------------------------------
URL = "https://todo.demo.onprem.anyship.cloud/healthz"


def test_wait_healthy_retries_until_it_gets_200():
    answers, pauses = iter([None, 502, 502, 200]), []
    ok, status = wait_healthy(URL, fetch=lambda u, v: next(answers), sleep=pauses.append, delay=2.0)
    assert (ok, status) == (True, 200) and pauses == [2.0, 2.0, 2.0]


def test_wait_healthy_gives_up_and_reports_the_last_status_without_a_pointless_last_pause():
    pauses = []
    ok, status = wait_healthy(URL, attempts=3, fetch=lambda u, v: 503, sleep=pauses.append)
    assert (ok, status) == (False, 503) and len(pauses) == 2


def test_wait_healthy_passes_the_tls_choice_through_and_requires_https():
    seen = []
    wait_healthy(URL, attempts=1, verify_tls=False, fetch=lambda u, v: seen.append(v) or 200)
    assert seen == [False]
    with pytest.raises(ValueError):
        wait_healthy("http://todo.example.com/healthz")


def test_status_maps_http_errors_to_their_code_and_connection_problems_to_none(monkeypatch):
    def raise_http(*a, **k):
        raise urllib.error.HTTPError(URL, 502, "bad gateway", {}, None)

    def raise_conn(*a, **k):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(compose_host.urllib.request, "urlopen", raise_http)
    assert compose_host._status(URL, True) == 502
    monkeypatch.setattr(compose_host.urllib.request, "urlopen", raise_conn)
    assert compose_host._status(URL, True) is None


# --- 이미 배포된 앱을 살펴보고 바꾸고 지우는 도구 -----------------------------------------------
def lifecycle():
    from fakes import LifecycleServer

    server = LifecycleServer()
    ssh = SshRunner(SshConnection("3.38.88.141", Path("/key")), runner=server)
    return ComposeHost(ssh, popen=server.popen), server


def deployed(tag="aaaaaaa"):
    host, server = lifecycle()
    host.write_stack("todo", stack(image_tag=tag))
    return host, server


def test_exists_follows_the_compose_file_on_the_server():
    host, server = lifecycle()
    assert not host.exists("todo")
    host.write_stack("todo", stack())
    assert host.exists("todo")


def test_the_current_tag_is_read_from_the_app_line_and_not_from_the_database_line():
    host, server = deployed("aaaaaaa")
    assert "postgres:16-alpine" in server.files["/opt/apps/todo/compose.yaml"].decode()
    assert host.current_image_tag("todo") == "aaaaaaa"
    assert lifecycle()[0].current_image_tag("todo") is None  # 배포된 적이 없다


def test_set_image_tag_changes_only_the_app_image_line():
    host, server = deployed("aaaaaaa")
    before = {path: data for path, data in server.files.items()}
    assert host.set_image_tag("todo", "bbbbbbb").ok
    old = before["/opt/apps/todo/compose.yaml"].decode().splitlines()
    new = server.files["/opt/apps/todo/compose.yaml"].decode().splitlines()
    assert [(a, b) for a, b in zip(old, new, strict=True) if a != b] == [
        ('    image: "todo:aaaaaaa"', '    image: "todo:bbbbbbb"')
    ]
    assert len(old) == len(new)
    for name in (".env", "app.env"):  # 비밀 파일은 건드리지 않는다
        assert server.files[f"/opt/apps/todo/{name}"] == before[f"/opt/apps/todo/{name}"]
    assert host.current_image_tag("todo") == "bbbbbbb"


def test_set_image_tag_writes_nothing_when_the_app_line_is_missing_or_appears_twice():
    host, server = deployed()
    path = "/opt/apps/todo/compose.yaml"
    text = server.files[path].decode()
    for broken in (
        text.replace('image: "todo:aaaaaaa"', 'image: "other:aaaaaaa"'),
        text.replace("  db:", '    image: "todo:aaaaaaa"\n  db:'),
    ):
        server.files[path] = broken.encode()
        result = host.set_image_tag("todo", "bbbbbbb")
        assert not result.ok and server.files[path] == broken.encode()


@pytest.mark.parametrize("tag", ["latest", "AAAAAAA", "aaaaaa", "aaaaaaa; echo", ""])
def test_set_image_tag_rejects_a_tag_that_is_not_a_commit_sha(tag):
    host, server = deployed()
    count = len(server.commands)
    with pytest.raises(ValueError):
        host.set_image_tag("todo", tag)
    assert len(server.commands) == count


def test_running_services_and_image_presence_come_from_the_server():
    host, server = deployed()
    assert host.running_services("todo") == set()
    host.up("todo")
    assert host.running_services("todo") == {"web", "db"}
    server.images.add("todo:aaaaaaa")
    assert host.image_present("todo:aaaaaaa") and not host.image_present("todo:bbbbbbb")
    with pytest.raises(ValueError):
        host.image_present("todo:latest")


def test_down_removes_the_volumes_and_the_directory_removal_targets_exactly_the_app():
    host, server = deployed()
    host.down("todo")
    assert server.commands[-1] == [
        "docker",
        "compose",
        "--project-directory",
        "/opt/apps/todo",
        "down",
        "--volumes",
        "--remove-orphans",
    ]
    host.remove_dir("todo")
    assert server.commands[-1] == ["rm", "-r", "-f", "/opt/apps/todo"]
    assert not any(path.startswith("/opt/apps/todo/") for path in server.files)


@pytest.mark.parametrize("app", ["..", "../etc", "a/b", "Todo", "", "a b", "todo/../x"])
def test_destructive_tools_refuse_names_that_could_point_elsewhere(app):
    host, server = lifecycle()
    for call in (host.remove_dir, host.down, host.exists):
        with pytest.raises(ValueError):
            call(app)
    assert server.commands == []

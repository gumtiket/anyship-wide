import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fakes import Log

from anyship_adapters import image_builder as ib
from anyship_adapters.image_builder import MAX_LINE, BuildError, BuiltImage, ImageBuilder

APP, SHA = "todo", "abc1234"
IMAGE = f"{APP}:{SHA}"
# 비밀 스캐너 오탐을 피하려고 조각으로 만든 가짜 토큰
FAKE_TOKEN = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.fixture(autouse=True)
def clean_locks():
    yield
    ib._ACTIVE.clear()  # 실패한 시험이 빌드 중 표시를 남겨 다른 시험에 번지지 않게 한다


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "src"
    for name, text in {
        "Dockerfile": "FROM scratch\n",
        "app.py": "print(1)",
        ".gitignore": "x",
        ".git/config": "secret-remote",
        ".env": "TOKEN=abc",
        ".env.local": "X=1",
        ".env.production": "Y=2",
        "sub/keep.txt": "k",
        "sub/.env": "Z=3",
        "sub/.git/HEAD": "ref",
        "docker/Dockerfile.prod": "FROM scratch\n",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


class Child:
    def __init__(self, lines=(), code=0, hold=None):
        self._lines, self.code, self.hold, self.killed = list(lines), code, hold, False
        self.stdout = self._read()

    def _read(self):
        for line in self._lines:
            yield line + "\n"
        if self.hold is not None:
            self.hold.wait(5)

    def wait(self):
        return -9 if self.killed else self.code

    def kill(self):
        self.killed = True
        if self.hold is not None:
            self.hold.set()


class Popen:
    """빌드가 시작되는 순간의 컨텍스트(파일과 링크)를 기록한다. 끝나면 임시 폴더가 지워지기 때문이다."""

    def __init__(self, script=None):
        self.script = script or (lambda args: Child(["ok"]))
        self.calls, self.snapshots, self.children = [], [], []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        context = Path(args[-1])
        files = sorted(
            p.relative_to(context).as_posix()
            for p in context.rglob("*")
            if p.is_file() or p.is_symlink()
        )
        links = sorted(
            p.relative_to(context).as_posix() for p in context.rglob("*") if p.is_symlink()
        )
        self.snapshots.append(
            {"context": context, "work": context.parent, "files": files, "links": links}
        )
        child = self.script(list(args))
        self.children.append(child)
        return child


class Run:
    """docker image inspect/ls/rm를 흉내 낸다."""

    def __init__(self, inspect="sha256:abc123 amd64\n", listing="", remove_ok=True, inspect_code=0):
        self.inspect, self.listing, self.remove_ok, self.inspect_code, self.calls = (
            inspect,
            listing,
            remove_ok,
            inspect_code,
            [],
        )

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        sub = args[2] if len(args) > 2 and args[1] == "image" else ""
        if sub == "inspect":
            return subprocess.CompletedProcess(
                args, self.inspect_code, stdout=self.inspect, stderr=""
            )
        if sub == "ls":
            return subprocess.CompletedProcess(args, 0, stdout=self.listing, stderr="")
        return subprocess.CompletedProcess(args, 0 if self.remove_ok else 1, stdout="", stderr="")


def builder(popen=None, run=None, **kwargs):
    return ImageBuilder(popen=popen or Popen(), run=run or Run(), **kwargs)


def expect_error(call, code):
    with pytest.raises(BuildError) as caught:
        call()
    assert caught.value.error.code == code
    return caught.value


def make_link(link: Path, target: Path):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없다")


# --- 입력 검증 ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "app, sha",
    [
        ("Bad Name", SHA),
        ("ab", SHA),
        ("1abc", SHA),
        ("", SHA),
        ("a" * 64, SHA),
        ("todo;rm", SHA),
        (APP, "xyz"),
        (APP, "abc12"),
        (APP, "A" * 7),
        (APP, "g" * 7),
        (APP, "a" * 41),
        (APP, ""),
    ],
)
def test_a_bad_app_name_or_commit_is_refused_before_anything_runs(source, app, sha):
    popen = Popen()
    expect_error(lambda: builder(popen).build(source, app, sha, Log()), "invalid_build_input")
    assert popen.calls == []


# "sub/../Dockerfile"은 소스 안에 머무는 경로지만 `..`가 들어 있어서 거부한다(경로 해석에 기대지 않는 정책).
@pytest.mark.parametrize(
    "dockerfile",
    [
        "",
        "../Dockerfile",
        "sub/../Dockerfile",
        "/etc/passwd",
        "a/../../x",
        "sub\\x",
        "missing",
        "sub",
    ],
)
def test_a_dockerfile_that_is_not_a_regular_file_inside_the_source_is_refused(source, dockerfile):
    popen = Popen()
    expect_error(
        lambda: builder(popen).build(source, APP, SHA, Log(), dockerfile=dockerfile),
        "invalid_build_input",
    )
    assert popen.calls == []


def test_a_source_that_is_not_a_folder_is_refused(source):
    expect_error(lambda: builder().build(source / "app.py", APP, SHA, Log()), "invalid_build_input")
    expect_error(lambda: builder().build(source / "nope", APP, SHA, Log()), "invalid_build_input")


def test_a_dockerfile_that_is_a_link_is_refused(source, tmp_path):
    (source / "Dockerfile").unlink()
    outside = tmp_path / "outside.Dockerfile"
    outside.write_text("FROM scratch\n")
    make_link(source / "Dockerfile", outside)
    popen = Popen()
    expect_error(lambda: builder(popen).build(source, APP, SHA, Log()), "invalid_build_input")
    assert popen.calls == []


# --- 컨텍스트 복사 ------------------------------------------------------------------------------
def test_git_and_env_files_never_reach_the_build_context_at_any_depth(source):
    popen = Popen()
    builder(popen).build(source, APP, SHA, Log())
    assert popen.snapshots[0]["files"] == [
        ".gitignore",
        "Dockerfile",
        "app.py",
        "docker/Dockerfile.prod",
        "sub/keep.txt",
    ]


def test_the_original_source_is_left_untouched(source):
    before = sorted(p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file())
    builder().build(source, APP, SHA, Log())
    assert (
        sorted(p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()) == before
    )


def test_links_are_copied_as_links_and_never_followed(source, tmp_path):
    secret = tmp_path / "server-secret.txt"
    secret.write_text("do not leak")
    make_link(source / "leak", secret)
    popen = Popen()
    builder(popen).build(source, APP, SHA, Log())
    assert popen.snapshots[0]["links"] == ["leak"]  # 내용이 아니라 링크 그대로 복사된다


def test_a_source_that_is_too_large_is_refused_before_building(source, monkeypatch):
    monkeypatch.setattr(ib, "MAX_CONTEXT_BYTES", 5)
    popen = Popen()
    expect_error(lambda: builder(popen).build(source, APP, SHA, Log()), "build_context_too_large")
    assert popen.calls == []


def test_excluded_files_do_not_count_toward_the_size_limit(source, monkeypatch):
    (source / ".git" / "pack").write_text("x" * 5000)
    (source / ".env.big").write_text("y" * 5000)
    monkeypatch.setattr(ib, "MAX_CONTEXT_BYTES", 200)
    builder().build(source, APP, SHA, Log())


def test_the_temporary_copy_is_removed_after_success_and_after_failure(source):
    ok, bad = Popen(), Popen(lambda args: Child(["boom"], 1))
    builder(ok).build(source, APP, SHA, Log())
    expect_error(lambda: builder(bad).build(source, APP, SHA, Log()), "docker_build_failed")
    assert not ok.snapshots[0]["work"].exists() and not bad.snapshots[0]["work"].exists()


# --- 실행 ---------------------------------------------------------------------------------------
def test_the_docker_command_targets_the_amd64_image_with_the_dockerfile_inside_the_copy(source):
    popen = Popen()
    builder(popen).build(source, APP, SHA, Log(), dockerfile="docker/Dockerfile.prod")
    args, context = popen.calls[0][0], popen.snapshots[0]["context"]
    assert args[:2] == ["docker", "build"] and ["--platform", "linux/amd64"] == args[2:4]
    assert args[args.index("--tag") + 1] == IMAGE
    assert args[args.index("--file") + 1] == str(context / "docker" / "Dockerfile.prod") and args[
        -1
    ] == str(context)


def test_the_process_gets_no_service_credentials_and_cannot_wait_for_input(source, monkeypatch):
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_PROFILE",
        "GITHUB_TOKEN",
        "APP_TOKEN_KEY",
    ):
        monkeypatch.setenv(name, "service-value")
    popen = Popen()
    builder(popen).build(source, APP, SHA, Log())
    kwargs = popen.calls[0][1]
    assert set(kwargs["env"]) <= {"PATH", "HOME", "LANG"} and kwargs["stdin"] == subprocess.DEVNULL


def test_progress_lines_are_forwarded_with_secrets_masked_and_blank_lines_dropped(source):
    log = Log()
    builder(Popen(lambda args: Child(["", f"cloning with {FAKE_TOKEN}", "  ", "done"]))).build(
        source, APP, SHA, log
    )
    assert FAKE_TOKEN not in log.text() and "***" in log.text()
    messages = [(e.step, e.name, e.message) for e in log.events]
    assert messages[0][:2] == (1, "빌드 준비") and (2, "이미지 빌드", "done") in messages
    assert not any(m[2].strip() == "" for m in messages)


def test_a_very_long_line_is_truncated(source):
    log = Log()
    builder(Popen(lambda args: Child(["x" * 5000]))).build(source, APP, SHA, log)
    assert max(len(e.message) for e in log.events) <= MAX_LINE


@pytest.mark.parametrize(
    "lines, code, retryable",
    [
        (["Step 1", "ERROR: pip failed"], "docker_build_failed", False),
        (
            ["ERROR: Cannot connect to the Docker daemon at unix:///var/run/docker.sock"],
            "docker_unavailable",
            True,
        ),
        (
            ["permission denied while trying to connect to the Docker daemon socket"],
            "docker_unavailable",
            True,
        ),
    ],
)
def test_a_failed_build_is_reported_with_a_code_and_the_masked_tail(source, lines, code, retryable):
    popen = Popen(lambda args: Child(lines + [f"token {FAKE_TOKEN}"], 1))
    error = expect_error(lambda: builder(popen).build(source, APP, SHA, Log()), code)
    assert error.error.retryable == retryable and lines[-1] in error.tail
    assert FAKE_TOKEN not in error.tail and FAKE_TOKEN not in error.error.model_dump_json()


def test_only_the_last_lines_of_a_failed_build_are_kept(source):
    popen = Popen(lambda args: Child([f"line {n}" for n in range(100)], 1))
    error = expect_error(
        lambda: builder(popen).build(source, APP, SHA, Log()), "docker_build_failed"
    )
    assert (
        error.tail.splitlines()[-1] == "line 99" and len(error.tail.splitlines()) == ib.TAIL_LINES
    )


def test_a_missing_docker_binary_is_reported(source):
    def missing(args, **kwargs):
        raise FileNotFoundError("docker")

    expect_error(lambda: builder(missing).build(source, APP, SHA, Log()), "docker_not_found")


def test_a_build_that_takes_too_long_is_killed_and_reported(source):
    hold, children = threading.Event(), []

    def script(args):
        child = Child(["working"], hold=hold)
        children.append(child)
        return child

    error = expect_error(
        lambda: builder(Popen(script), timeout=0.1).build(source, APP, SHA, Log()), "build_timeout"
    )
    assert error.error.retryable and children[-1].killed
    builder().build(source, APP, SHA, Log())  # 시간 초과 뒤에도 같은 빌드를 다시 시작할 수 있다


# --- 동시 빌드와 취소 ---------------------------------------------------------------------------
def test_the_same_app_and_commit_cannot_be_built_twice_at_once(source):
    release, started = threading.Event(), threading.Event()

    def slow(args):
        started.set()
        return Child(["working"], hold=release)

    first = threading.Thread(target=lambda: builder(Popen(slow)).build(source, APP, SHA, Log()))
    first.start()
    try:
        assert started.wait(5)
        error = expect_error(lambda: builder().build(source, APP, SHA, Log()), "build_in_progress")
        assert error.error.retryable
        builder().build(source, APP, "def5678", Log())  # 다른 커밋은 막지 않는다
        builder().build(source, "other-app", SHA, Log())  # 다른 앱도 막지 않는다
    finally:
        release.set()
        first.join(5)
    builder().build(source, APP, SHA, Log())  # 끝난 뒤에는 다시 빌드할 수 있다


def test_a_failed_build_releases_the_lock(source):
    expect_error(
        lambda: builder(Popen(lambda args: Child([], 1))).build(source, APP, SHA, Log()),
        "docker_build_failed",
    )
    builder().build(source, APP, SHA, Log())


def test_cancelling_through_the_log_callback_kills_docker_and_releases_the_lock(source):
    popen = Popen(lambda args: Child(["a", "b"]))

    def cancel(event):
        if event.message == "a":
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError):
        builder(popen).build(source, APP, SHA, cancel)
    assert popen.children[-1].killed
    builder().build(source, APP, SHA, Log())


# --- 빌드 뒤 확인 -------------------------------------------------------------------------------
def test_the_result_names_the_image_and_its_id(source):
    run = Run()
    built = builder(run=run).build(source, APP, SHA, Log())
    assert built == BuiltImage(image=IMAGE, image_id="sha256:abc123")
    assert run.calls[0][:3] == ["docker", "image", "inspect"] and run.calls[0][-1] == IMAGE


def test_an_image_that_is_not_amd64_is_rejected(source):
    expect_error(
        lambda: builder(run=Run(inspect="sha256:abc arm64\n")).build(source, APP, SHA, Log()),
        "wrong_architecture",
    )


@pytest.mark.parametrize(
    "run", [Run(inspect_code=1), Run(inspect=""), Run(inspect="only-one-token\n")]
)
def test_an_image_that_cannot_be_confirmed_is_reported(source, run):
    expect_error(lambda: builder(run=run).build(source, APP, SHA, Log()), "docker_build_failed")


# --- 오래된 이미지 정리 -------------------------------------------------------------------------
LISTING = (
    "aaaaaaa\t2026-10-01 10:00:00 +0000 UTC\nbbbbbbb\t2026-10-03 10:00:00 +0000 UTC\n"
    "ccccccc\t2026-10-02 10:00:00 +0000 UTC\nlatest\t2026-10-09 10:00:00 +0000 UTC\n"
    "ddddddd\t2026-10-04 10:00:00 +0000 UTC\n"
)


def test_prune_keeps_the_newest_images_and_never_touches_other_tags():
    run = Run(listing=LISTING)
    removed = builder(run=run).prune(APP, keep=2)
    assert removed == [
        f"{APP}:ccccccc",
        f"{APP}:aaaaaaa",
    ]  # 가장 최근 둘(ddddddd, bbbbbbb)만 남는다
    assert all("latest" not in " ".join(call) for call in run.calls if call[2] == "rm")
    assert run.calls[0][run.calls[0].index("--filter") + 1] == f"reference={APP}"


def test_prune_removes_nothing_when_there_are_not_more_than_keep():
    run = Run(listing=LISTING)
    assert builder(run=run).prune(APP, keep=10) == []
    assert not any(call[2] == "rm" for call in run.calls)


def test_prune_skips_images_that_are_in_use_without_failing():
    assert builder(run=Run(listing=LISTING, remove_ok=False)).prune(APP, keep=1) == []


@pytest.mark.parametrize("app, keep", [("Bad Name", 5), ("", 5), (APP, 0), (APP, -1)])
def test_prune_refuses_bad_arguments(app, keep):
    run = Run()
    with pytest.raises(ValueError):
        builder(run=run).prune(app, keep)
    assert run.calls == []


# --- 진짜 하위 프로세스 -------------------------------------------------------------------------
FAKE_DOCKER = f"""import os, sys
print("sub:", sys.argv[1])
print("aws secret in env:", os.environ.get("AWS_SECRET_ACCESS_KEY"))
print("stdin:", repr(sys.stdin.read()))
print("token:", "{FAKE_TOKEN}")
"""


def test_it_works_with_a_real_subprocess(source, tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "service-secret-value")
    script = tmp_path / "fake_docker.py"
    script.write_text(FAKE_DOCKER)

    def real(args, **kwargs):
        if os.name == "nt":  # Windows의 파이썬은 SYSTEMROOT 없이는 시작하지 못한다
            kwargs["env"] = {**kwargs["env"], "SYSTEMROOT": os.environ["SYSTEMROOT"]}
        return subprocess.Popen([sys.executable, str(script)] + args[1:], **kwargs)

    log = Log()
    built = builder(real).build(source, APP, SHA, log)
    assert built.image == IMAGE
    assert (
        "sub: build" in log.text()
        and "aws secret in env: None" in log.text()
        and "stdin: ''" in log.text()
    )
    assert FAKE_TOKEN not in log.text() and "token: ***" in log.text()

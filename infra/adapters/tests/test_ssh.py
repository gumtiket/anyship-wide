import shlex
import subprocess
from pathlib import Path

import pytest

from anyship_adapters.ssh import MAX_OUTPUT, SshConnection, SshRunner

KEY = Path("/home/ec2-user/.ssh/onprem_deploy")


class FakeRunner:
    """subprocess.run 대신 끼워 넣어서, 실제 접속 없이 만들어진 명령만 기록한다."""

    def __init__(self, returncode=0, stdout=b"", stderr=b"", error=None):
        self.calls = []
        self._done = (returncode, stdout, stderr)
        self._error = error

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self._error:
            raise self._error
        return subprocess.CompletedProcess(
            cmd, self._done[0], stdout=self._done[1], stderr=self._done[2]
        )

    @property
    def command(self):
        return self.calls[-1][0]

    @property
    def kwargs(self):
        return self.calls[-1][1]


def make(**kw):
    fake = FakeRunner(**kw)
    return SshRunner(SshConnection("3.38.88.141", KEY), runner=fake), fake


# --- 만들어지는 ssh 명령 -------------------------------------------------------------------
def test_command_targets_the_deploy_account_with_key_only_options():
    runner, fake = make()
    runner.run(["true"])
    cmd = fake.command
    assert cmd[0] == "ssh" and "deploy@3.38.88.141" in cmd
    options = " ".join(cmd)
    for expected in (
        f"-i {KEY}",
        "BatchMode=yes",
        "PasswordAuthentication=no",
        "IdentitiesOnly=yes",
        "-p 22",
    ):
        assert expected in options


@pytest.mark.parametrize(
    "args",
    [
        ["echo", "a; touch /tmp/x"],
        ["echo", "$(id)", "`id`"],
        ["echo", "it's", 'say "hi"'],
        ["echo", "a b", "c\nd"],
        ["echo", "&& reboot", "| cat", "> /etc/passwd"],
    ],
)
def test_every_argument_reaches_the_remote_shell_as_one_token(args):
    runner, fake = make()
    runner.run(args)
    assert shlex.split(fake.command[-1]) == args


def test_known_hosts_file_is_used_when_given():
    fake = FakeRunner()
    SshRunner(SshConnection("h.example.com", KEY), runner=fake, known_hosts=Path("/var/known")).run(
        ["true"]
    )
    assert f"UserKnownHostsFile={Path('/var/known')}" in " ".join(fake.command)


def test_only_a_minimal_environment_is_passed_to_ssh(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-forward")
    monkeypatch.setenv("GITHUB_TOKEN", "do-not-forward")
    runner, fake = make()
    runner.run(["true"])
    assert set(fake.kwargs["env"]) <= {"PATH", "HOME", "LANG"}


# --- 결과 -----------------------------------------------------------------------------------
def test_results_report_success_failure_and_connection_failure():
    assert make(returncode=0)[0].run(["true"]).ok
    failed = make(returncode=1)[0].run(["false"])
    assert not failed.ok and not failed.connection_failed
    assert make(returncode=255)[0].run(["true"]).connection_failed


def test_output_is_decoded_and_cut_to_a_safe_size():
    result = make(stdout=b"x" * (MAX_OUTPUT + 10), stderr=b"bad \xff byte")[0].run(["true"])
    assert len(result.stdout) == MAX_OUTPUT
    assert "bad" in result.stderr and "�" in result.stderr


def test_a_timeout_becomes_a_result_instead_of_an_exception():
    error = subprocess.TimeoutExpired("ssh", 1, output=b"partial")
    result = make(error=error)[0].run(["sleep", "99"], timeout=1)
    assert result.timed_out and not result.ok and result.returncode == 124
    assert result.stdout == "partial"


# --- 파일 쓰기 ------------------------------------------------------------------------------
def test_put_sends_the_content_on_stdin_never_on_the_command_line():
    runner, fake = make()
    runner.put("/opt/apps/todo/app.env", "SECRET_KEY='abc123'\n", mode="0600")
    assert fake.kwargs["input"] == b"SECRET_KEY='abc123'\n"
    assert "abc123" not in " ".join(fake.command)
    assert shlex.split(fake.command[-1])[-2:] == ["/opt/apps/todo/app.env", "0600"]


def test_put_creates_the_file_private_first_then_sets_the_mode_then_replaces():
    runner, fake = make()
    runner.put("/opt/apps/todo/app.env", "x", mode="0600")
    script = shlex.split(fake.command[-1])[2]
    assert script.index("umask 077") < script.index("chmod") < script.index("mv")


@pytest.mark.parametrize(
    "path, mode",
    [
        ("opt/x", "0644"),
        ("/opt/../etc/passwd", "0644"),
        ("/opt/a b", "0644"),
        ("/opt/a;b", "0644"),
        ("", "0644"),
        ("/opt/x", "644"),
        ("/opt/x", "0999"),
        ("/opt/x", "u+x"),
    ],
)
def test_put_rejects_unsafe_paths_and_modes_before_running_anything(path, mode):
    runner, fake = make()
    with pytest.raises(ValueError):
        runner.put(path, "x", mode=mode)
    assert fake.calls == []


# --- 입력 검증 ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "host, user, port",
    [
        ("-oProxyCommand=x", "deploy", 22),
        ("a b", "deploy", 22),
        ("a;ls", "deploy", 22),
        ("", "deploy", 22),
        ("h.example.com", "Root", 22),
        ("h.example.com", "a b", 22),
        ("h.example.com", "deploy", 0),
        ("h.example.com", "deploy", 70000),
    ],
)
def test_connection_values_that_could_reach_the_command_line_are_rejected(host, user, port):
    with pytest.raises(ValueError):
        SshConnection(host, KEY, user=user, port=port)


@pytest.mark.parametrize("args", [[], ["a\0b"]])
def test_empty_commands_and_nul_bytes_are_rejected_before_running(args):
    runner, fake = make()
    with pytest.raises(ValueError):
        runner.run(args)
    assert fake.calls == []


def test_a_stream_can_be_piped_in_without_loading_it_into_memory():
    runner, fake = make()
    stream = object()  # 실제 파이프 대신 같은 객체가 그대로 전달되는지만 본다
    runner.run(["docker", "load"], stdin=stream)
    assert fake.kwargs["stdin"] is stream and "input" not in fake.kwargs


def test_input_and_stdin_cannot_be_combined():
    runner, fake = make()
    with pytest.raises(ValueError):
        runner.run(["cat"], input="x", stdin=object())
    assert fake.calls == []

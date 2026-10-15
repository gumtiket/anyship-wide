"""Reusable runner contract: only the fixture knows which transport is under test.

The subprocess replacement executes commands on a private local POSIX sandbox;
it never starts SSH or contacts a server. Add an agent fixture to the parameter
list when that implementation exists, retaining these behavioral assertions.
"""

import io
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from anyship_adapters.ssh import MAX_OUTPUT, SshConnection, SshRunner


@pytest.fixture(params=["ssh"])
def transport(request, tmp_path):
    shell = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
    if not shell or not Path(shell).is_file():
        pytest.skip("A POSIX shell is needed for the local filesystem contract sandbox.")

    def remote(path):
        value = path.as_posix()
        return "/" + value[0].lower() + value[2:] if os.name == "nt" else value

    state = SimpleNamespace(fail_connection=False, fail_permissions=False)

    def process(command, **kwargs):
        if state.fail_connection:
            return subprocess.CompletedProcess(command, 255, b"", b"offline")
        # In the fake server, chmod failing simulates disk/permission failure before rename.
        remote_command = shlex.split(command[-1])
        if state.fail_permissions and remote_command[:2] == ["sh", "-c"]:
            return subprocess.run(
                [shell, "-c", "chmod() { return 1; }; " + remote_command[2], *remote_command[3:]],
                **kwargs,
            )
        return subprocess.run([shell, "-c", command[-1]], **kwargs)

    runner = SshRunner(SshConnection("example.com", Path("unused-key")), runner=process)
    return SimpleNamespace(
        runner=runner,
        state=state,
        path=tmp_path / "secret",
        remote=remote(tmp_path / "secret"),
        limit=MAX_OUTPUT,
    )


def test_argument_boundaries_and_input_stream(transport):
    runner = transport.runner
    literal = "value with spaces; $(not-a-command) 'quoted'"
    assert runner.run(["printf", "%s", literal]).stdout == literal
    assert runner.run(["cat"], input="utf8 내용").stdout == "utf8 내용"
    with io.BytesIO(b"stream") as stream:
        # BytesIO has no fileno; use a real temporary file for subprocess stdin.
        transport.path.write_bytes(stream.read())
    with transport.path.open("rb") as stream:
        assert runner.run(["cat"], stdin=stream).stdout == "stream"


def test_timeout_connection_failure_and_bounded_output(transport):
    result = transport.runner.run(["sleep", "1"], timeout=0.02)
    assert result.returncode == 124 and result.timed_out and not result.ok
    transport.state.fail_connection = True
    assert transport.runner.run(["true"]).connection_failed
    transport.state.fail_connection = False
    result = transport.runner.run(["sh", "-c", "head -c 100000 /dev/zero | tr '\\0' x"])
    assert len(result.stdout) == transport.limit and result.ok


def test_put_replaces_atomically_and_leaves_old_content_on_failure(transport):
    transport.path.write_text("old", encoding="utf-8")
    transport.state.fail_permissions = True
    assert not transport.runner.put(transport.remote, "secret-new", mode="0600").ok
    assert transport.path.read_text(encoding="utf-8") == "old"
    transport.state.fail_permissions = False
    assert transport.runner.put(transport.remote, "secret-new", mode="0600").ok
    assert transport.path.read_text(encoding="utf-8") == "secret-new"
    assert not Path(str(transport.path) + ".tmp").exists()
    if os.name != "nt":  # Windows ACLs do not implement POSIX mode bits.
        assert transport.path.stat().st_mode & 0o777 == 0o600

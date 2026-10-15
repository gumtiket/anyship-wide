"""오프라인 서비스 전달이 모든 호스트에서 LF/CRLF 바이트를 보존한다."""

import subprocess
import sys
from pathlib import Path

import pytest

from ai.detectors.repo import RepoView
from ai.gate.runner import DockerCliRunner, RunnerError
from ai.pipeline import _write_output
from ai.transform.context import prepare_context
from ai.transform.workspace import Workspace, make_diff


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_patch_and_output_preserve_original_bytes(tmp_path, newline):
    before = {"main.py": f"value = 1{newline}"}
    after = {"main.py": f"value = 2{newline}"}
    diff = make_diff(before, after)
    output = tmp_path / "changes.diff"
    _write_output(output, diff)
    assert output.read_bytes() == diff.encode("utf-8")
    with Workspace(before) as workspace:
        assert (workspace.root / "main.py").read_bytes() == before["main.py"].encode()
        workspace.apply(output.read_bytes().decode())
        assert workspace.read({"main.py"}) == after
        assert workspace.compile()


@pytest.mark.parametrize("operation", ["preflight", "build"])
def test_real_docker_gate_fails_explicitly_on_non_posix(monkeypatch, tmp_path, operation):
    runner = DockerCliRunner()

    def forbidden_call(*args, **kwargs):
        raise AssertionError("Non-POSIX rejection must happen before a Docker call")

    monkeypatch.setattr(runner, "_call", forbidden_call)
    monkeypatch.setattr("ai.gate.runner.os.name", "nt")
    with pytest.raises(RunnerError, match="docker_gate_requires_posix"):
        if operation == "preflight":
            runner.preflight()
        else:
            runner.build(tmp_path, "bronze-ai-gate:gate-" + "a" * 24)


def test_offline_pipeline_import_does_not_require_fcntl():
    code = """
import builtins
original_import = builtins.__import__
def without_fcntl(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError('fcntl unavailable on Windows')
    return original_import(name, *args, **kwargs)
builtins.__import__ = without_fcntl
from ai.pipeline import run_analysis
from ai.gate.runner import FakeRunner
assert callable(run_analysis)
assert FakeRunner().real is False
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_build_context_preserves_source_and_dockerfile_bytes(tmp_path, newline):
    source = f"value = 1{newline}"
    (tmp_path / "main.py").write_bytes(source.encode("utf-8"))
    dockerfile = "FROM python:3.12-slim\n"
    context = prepare_context(RepoView(tmp_path), "", dockerfile)
    try:
        root = Path(context.root)
        assert (root / "main.py").read_bytes() == source.encode("utf-8")
        assert (root / "Dockerfile").read_bytes() == dockerfile.encode("utf-8")
    finally:
        context.cleanup()

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from tests.github_manual import (
    ConfigurationError,
    PullRequestCreationError,
    run_manual_test,
)

URL = "https://github.com/owner/repo"


def test_created(capsys):
    result = {
        "status": "created",
        "repository": "owner/repo",
        "path": "workspace",
        "branch": "ai/example",
        "base_branch": "main",
        "diff_summary": "example.txt | 1 +",
        "pull_request": {"number": 1, "url": f"{URL}/pull/1"},
    }
    with patch("tests.github_manual.publish_workspace", return_value=result) as workflow:
        assert run_manual_test(["publish", URL, "workspace", "--pr"]) == 0
    workflow.assert_called_once_with(URL, Path("workspace"), open_pr=True)
    output = capsys.readouterr()
    assert result["pull_request"]["url"] in output.out
    assert result["diff_summary"] in output.out
    assert output.err == ""


@pytest.mark.parametrize("json_output", [False, True])
def test_pushed(capsys, json_output):
    result = {
        "status": "pushed",
        "repository": "owner/repo",
        "path": "workspace",
        "branch": "work",
        "base_branch": "main",
    }
    args = ["publish", URL, "workspace"]
    if json_output:
        args.append("--json")

    with patch("tests.github_manual.publish_workspace", return_value=result):
        assert run_manual_test(args) == 0
    output = capsys.readouterr()
    assert output.err == ""
    if json_output:
        assert json.loads(output.out) == result
    else:
        assert "owner/repo" in output.out


@pytest.mark.parametrize(
    "error,code",
    [
        (ConfigurationError("Missing settings: GITHUB_TOKEN"), 2),
        (ValueError("invalid URL"), 2),
        (subprocess.CalledProcessError(1, "git", stderr="private-token"), 1),
        (subprocess.TimeoutExpired("git", 120), 1),
        (requests.Timeout("private-token"), 1),
        (FileNotFoundError("private-token"), 1),
        (KeyboardInterrupt(), 130),
    ],
)
def test_errors(capsys, error, code):
    with patch("tests.github_manual.publish_workspace", side_effect=error):
        assert run_manual_test(["publish", URL, "workspace", "--json"]) == code
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["status"] == "error"
    assert "private-token" not in output.err


def test_pr_failure_includes_recovery_context(capsys):
    error = PullRequestCreationError("owner/repo", "ai/recovery")
    with patch("tests.github_manual.publish_workspace", side_effect=error):
        assert run_manual_test(["publish", URL, "workspace", "--json"]) == 3
    result = json.loads(capsys.readouterr().err)
    assert result["repository"] == "owner/repo"
    assert result["branch"] == "ai/recovery"


@pytest.mark.parametrize(
    "args,code",
    [(["--help"], 0), ([], 2), ([URL, "--unknown"], 2)],
)
def test_parser_does_not_start_workflow(args, code):
    with patch("tests.github_manual.publish_workspace") as workflow:
        with pytest.raises(SystemExit) as caught:
            run_manual_test(args)
    assert caught.value.code == code
    workflow.assert_not_called()


def test_module_entrypoint():
    result = subprocess.run(
        [sys.executable, "-B", "-m", "tests.github_manual", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0
    assert b"prepare" in result.stdout
    assert b"publish" in result.stdout

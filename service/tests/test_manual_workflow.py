import subprocess
from unittest.mock import patch

import pytest
import requests

from github import GitCommandError, Workspace
from github.changes import commit_changes, stage_changes
from github.repository import run_git
from tests.github_manual import (
    PullRequestCreationError,
    Settings,
    prepare_workspace,
    publish_workspace,
)

URL = "https://github.com/owner/repo"
SETTINGS = Settings("test-token", "owner/repo")


@pytest.fixture
def workspace(tmp_path):
    repo_path = tmp_path / "repository"
    repo_path.mkdir()
    run_git(["init", "--initial-branch=main"], cwd=repo_path)
    (repo_path / "README.md").write_text("original", encoding="utf-8")
    stage_changes(repo_path)
    commit_changes(repo_path)
    run_git(["remote", "add", "origin", URL], cwd=repo_path)
    run_git(["checkout", "-b", "work"], cwd=repo_path)
    return repo_path


@pytest.fixture
def remote_operations():
    with (
        patch("github.pull_request.get_default_branch", return_value="main"),
        patch("github.changes.push_branch") as push,
        patch(
            "github.pull_request.create_pull_request",
            return_value={"number": 1, "url": URL + "/pull/1"},
        ) as pr,
    ):
        yield push, pr


def test_prepare_new_directory(tmp_path, remote_operations):
    destination = tmp_path / "nested" / "repository"
    with (
        patch("tests.github_manual.GitHubRepository.clone") as clone,
        patch("tests.github_manual.GitHubRepository.create_branch") as branch,
    ):
        clone.side_effect = lambda **kwargs: destination.mkdir(parents=True)
        branch.return_value = Workspace("owner/repo", destination, "work")
        result = prepare_workspace(URL, destination, branch="work", settings=SETTINGS)
    assert destination.exists()
    assert result["status"] == "prepared"
    clone.assert_called_once_with(branch="main")
    branch.assert_called_once_with("work")
    remote_operations[0].assert_not_called()


def test_prepare_reuses_matching_workspace_without_touching_changes(workspace, remote_operations):
    target = workspace / "README.md"
    target.write_text("my edit", encoding="utf-8")
    result = prepare_workspace(URL, workspace, settings=SETTINGS)
    assert result["branch"] == "work"
    assert target.read_text(encoding="utf-8") == "my edit"
    remote_operations[0].assert_not_called()


def test_prepare_refuses_existing_unrelated_directory(tmp_path, remote_operations):
    target = tmp_path / "keep.txt"
    target.write_text("keep", encoding="utf-8")
    with pytest.raises((ValueError, GitCommandError)):
        prepare_workspace(URL, tmp_path, settings=SETTINGS)
    assert target.read_text(encoding="utf-8") == "keep"


def test_publish_commits_text_and_new_code(workspace, remote_operations):
    (workspace / "README.md").write_text("updated", encoding="utf-8")
    (workspace / "hello.py").write_text('print("hello")', encoding="utf-8")
    result = publish_workspace(URL, workspace, settings=SETTINGS)
    assert result["status"] == "pushed"
    assert workspace.exists()
    assert run_git(["show", "HEAD:README.md"], cwd=workspace) == "updated"
    assert run_git(["show", "HEAD:hello.py"], cwd=workspace) == 'print("hello")'
    assert run_git(["status", "--porcelain"], cwd=workspace) == ""
    remote_operations[0].assert_called_once_with(workspace, "work", "test-token")
    remote_operations[1].assert_not_called()


def test_publish_pushes_existing_commits_without_new_changes(workspace, remote_operations):
    before = run_git(["rev-parse", "HEAD"], cwd=workspace)
    publish_workspace(URL, workspace, settings=SETTINGS)
    assert run_git(["rev-parse", "HEAD"], cwd=workspace) == before
    remote_operations[0].assert_called_once()


def test_optional_pr(workspace, remote_operations):
    result = publish_workspace(URL, workspace, open_pr=True, settings=SETTINGS)
    assert result["status"] == "created"
    remote_operations[1].assert_called_once_with(
        "owner/repo",
        "work",
        "main",
        "test-token",
        title="Automated Code Changes",
        body="코드 수정 도구가 생성한 자동 코드 변경 사항입니다.",
        draft=True,
    )


def test_pr_failure_keeps_workspace(workspace, remote_operations):
    remote_operations[1].side_effect = requests.Timeout()
    with pytest.raises(PullRequestCreationError) as caught:
        publish_workspace(URL, workspace, open_pr=True, settings=SETTINGS)
    assert caught.value.branch == "work"
    assert workspace.exists()


@pytest.mark.parametrize(
    "invalid_state", ["base", "detached", "remote", "push_remote", "subdirectory"]
)
def test_publish_rejects_wrong_workspace(workspace, remote_operations, invalid_state):
    if invalid_state == "base":
        run_git(["checkout", "main"], cwd=workspace)
    elif invalid_state == "detached":
        run_git(["checkout", "--detach"], cwd=workspace)
    elif invalid_state == "remote":
        run_git(["remote", "set-url", "origin", "https://github.com/other/repo"], cwd=workspace)
    elif invalid_state == "push_remote":
        run_git(
            ["remote", "set-url", "--push", "origin", "https://github.com/other/repo"],
            cwd=workspace,
        )
    else:
        workspace = workspace / "subdir"
        workspace.mkdir()
    with pytest.raises(ValueError):
        publish_workspace(URL, workspace, settings=SETTINGS)
    remote_operations[0].assert_not_called()


def test_disallowed_repository(tmp_path, remote_operations):
    with pytest.raises(ValueError):
        prepare_workspace("https://github.com/unknown/repo", tmp_path, settings=SETTINGS)


def test_prepare_rejects_branch_mismatch(workspace, remote_operations):
    with pytest.raises(ValueError):
        prepare_workspace(URL, workspace, branch="other", settings=SETTINGS)
    assert run_git(["branch", "--show-current"], cwd=workspace) == "work"


def test_push_failure_preserves_commit_for_retry(workspace, remote_operations):
    (workspace / "retry.txt").write_text("retry", encoding="utf-8")
    remote_operations[0].side_effect = subprocess.CalledProcessError(1, "git")
    with pytest.raises(GitCommandError):
        publish_workspace(URL, workspace, open_pr=True, settings=SETTINGS)
    assert run_git(["show", "HEAD:retry.txt"], cwd=workspace) == "retry"
    remote_operations[1].assert_not_called()

import subprocess
import traceback
from unittest.mock import Mock, patch

import pytest
import requests

from github import (
    AuthenticationError,
    CommitResult,
    GitCommandError,
    GitHubAPIError,
    GitHubRepository,
    InvalidRepositoryError,
    PullRequest,
    PushResult,
    ValidationError,
)
from github.changes import commit_changes, stage_changes
from github.repository import run_git

URL = "https://github.com/owner/repo"


@pytest.fixture
def repo(tmp_path):
    run_git(["init", "--initial-branch=main"], cwd=tmp_path)
    (tmp_path / "readme.txt").write_text("original", encoding="utf-8")
    stage_changes(tmp_path)
    commit_changes(tmp_path)
    run_git(["remote", "add", "origin", URL], cwd=tmp_path)
    return GitHubRepository(URL, tmp_path, "private-token")


def test_construct_does_not_read_configuration_or_run_operations(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with patch("github.repository.run_git") as git:
        client = GitHubRepository(URL, tmp_path / "new", "private-token")
    git.assert_not_called()
    assert not client.path.exists()
    assert "private-token" not in repr(client)


def test_local_edit_commit_and_no_changes(repo):
    workspace = repo.create_branch("work/custom")
    assert workspace.branch == "work/custom"
    (repo.path / "readme.txt").write_text("changed", encoding="utf-8")
    (repo.path / "new.py").write_text('print("hello")', encoding="utf-8")
    assert "new.py" in repo.get_changes()
    assert "changed" in repo.get_diff()
    result = repo.commit(
        "feat: custom change", author_name="Example", author_email="example@example.com"
    )
    assert isinstance(result, CommitResult)
    assert result.created and result.sha
    assert "new.py" in result.summary
    assert run_git(["log", "-1", "--format=%s|%an|%ae"], cwd=repo.path) == (
        "feat: custom change|Example|example@example.com"
    )
    assert repo.get_changes() == ""
    assert repo.commit("nothing") == CommitResult(False, None, "")
    with pytest.raises(subprocess.CalledProcessError):
        run_git(["config", "--local", "--get", "user.name"], cwd=repo.path)


def test_existing_git_identity_is_used_when_author_omitted(repo):
    run_git(["config", "user.name", "Existing User"], cwd=repo.path)
    run_git(["config", "user.email", "existing@example.com"], cwd=repo.path)
    (repo.path / "new.txt").write_text("new", encoding="utf-8")
    repo.commit("use existing identity")
    assert run_git(["log", "-1", "--format=%an"], cwd=repo.path) == "Existing User"


def test_clone_delegates_without_overwriting(tmp_path):
    client = GitHubRepository(URL, tmp_path / "nested" / "repo", "token")
    with (
        patch("github.pull_request.get_default_branch", return_value="develop"),
        patch("github.repository.clone_repository", return_value=client.path) as clone,
    ):
        assert client.clone() == client.path
    clone.assert_called_once_with("owner/repo", "develop", client.path, "token")
    client.path.mkdir()
    with pytest.raises(InvalidRepositoryError):
        client.clone()


def test_push_and_custom_pull_request(repo):
    repo.create_branch("work/custom")
    with (
        patch("github.pull_request.get_default_branch", return_value="main"),
        patch("github.changes.push_branch") as push,
    ):
        assert repo.push() == PushResult("owner/repo", "work/custom")
    push.assert_called_once_with(repo.path, "work/custom", "private-token")

    response = Mock()
    response.json.return_value = {"number": 4, "html_url": URL + "/pull/4"}
    with patch("github.pull_request.requests.post", return_value=response) as post:
        result = repo.create_pull_request("Custom title", body="Details", base="main", draft=False)
    assert result == PullRequest(4, URL + "/pull/4")
    assert post.call_args.kwargs["json"] == {
        "title": "Custom title",
        "body": "Details",
        "head": "work/custom",
        "base": "main",
        "draft": False,
    }


def test_default_branch_push_is_rejected(repo):
    with (
        patch("github.pull_request.get_default_branch", return_value="main"),
        patch("github.changes.push_branch") as push,
    ):
        with pytest.raises(InvalidRepositoryError):
            repo.push()
    push.assert_not_called()


@pytest.mark.parametrize(
    "status,error_type",
    [
        (401, AuthenticationError),
        (403, AuthenticationError),
        (404, GitHubAPIError),
        (500, GitHubAPIError),
    ],
)
def test_http_errors_are_typed_and_do_not_print_token(tmp_path, status, error_type):
    client = GitHubRepository(URL, tmp_path, "private-token")
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError("private-token", response=response)
    with patch("github.pull_request.requests.get", side_effect=error):
        with pytest.raises(error_type) as caught:
            client.get_default_branch()
    assert "private-token" not in "".join(traceback.format_exception(caught.value))


def test_git_failure_is_typed_and_preserves_files(repo):
    with patch(
        "github.repository.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git", stderr="private-token"),
    ):
        with pytest.raises(GitCommandError) as caught:
            repo.inspect()
    assert "private-token" not in "".join(traceback.format_exception(caught.value))
    assert (repo.path / "readme.txt").exists()


@pytest.mark.parametrize("url", ["https://example.com/a/b", "https://github.com/../repo"])
def test_invalid_repository_url(tmp_path, url):
    with pytest.raises(InvalidRepositoryError):
        GitHubRepository(url, tmp_path, "token")


def test_blank_token(tmp_path):
    with pytest.raises(AuthenticationError):
        GitHubRepository(URL, tmp_path, " ")


def test_blank_commit_and_pr_title_do_not_mutate(repo):
    with pytest.raises(ValidationError):
        repo.commit(" ")
    with pytest.raises(ValidationError):
        repo.create_pull_request(" ")


def test_unexpected_response_is_api_error(tmp_path):
    client = GitHubRepository(URL, tmp_path, "token")
    response = Mock()
    response.json.return_value = {}
    with patch("github.pull_request.requests.get", return_value=response):
        with pytest.raises(GitHubAPIError):
            client.get_default_branch()

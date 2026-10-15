import pytest

from github.repository import parse_repo_url


@pytest.mark.parametrize("suffix", ["", ".git", "/"])
def test_valid_repo_url(suffix):
    url = f"https://github.com/owner/example{suffix}"
    assert parse_repo_url(url) == "owner/example"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/test/repo",
        "https://github.com/test",
        "https://github.com/a/b/c",
        "http://github.com/test/repo",
        "https://user:secret@github.com/a/b",
        "https://github.com/a/b?x=1",
        "https://github.com/a/b#x",
    ],
)
def test_invalid_repo_url(url):
    with pytest.raises(ValueError):
        parse_repo_url(url)


def test_clone_repository_without_depth_keeps_the_full_history_and_the_token_out_of_the_command(
    tmp_path, monkeypatch
):
    from github import repository

    calls = []
    monkeypatch.setattr(
        repository, "run_git", lambda args, cwd, token=None: calls.append((args, cwd, token)) or ""
    )
    destination = tmp_path / "repo"
    assert repository.clone_repository("owner/example", "main", destination, "tok") == destination
    ((args, cwd, token),) = calls
    assert args == [
        "clone",
        "--single-branch",
        "--branch",
        "main",
        "https://github.com/owner/example.git",
        str(destination),
    ]
    assert (cwd, token) == (tmp_path, "tok") and "tok" not in " ".join(args)


def test_clone_repository_with_depth_fetches_only_the_latest_commits(tmp_path, monkeypatch):
    from github import repository

    calls = []
    monkeypatch.setattr(
        repository, "run_git", lambda args, cwd, token=None: calls.append(args) or ""
    )
    repository.clone_repository("owner/example", "dev", tmp_path / "repo", "tok", depth=1)
    assert calls[0][:7] == [
        "clone",
        "--single-branch",
        "--branch",
        "dev",
        "--depth",
        "1",
        "https://github.com/owner/example.git",
    ]

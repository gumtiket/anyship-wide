import os
import re
import shutil
import subprocess

import pytest

from app import source
from app.source import GitHubCloneSource, LocalFolderSource, SourceError

SPEC = "app: todo\nport: 8080\n"


def make(root, name="todo", extra=None, spec=SPEC, dockerfile="FROM python:3.12-slim\n"):
    folder = root / name
    (folder / "app").mkdir(parents=True)
    (folder / "Dockerfile").write_text(dockerfile)
    (folder / "deploy-spec.yaml").write_text(spec)
    (folder / "app" / "main.py").write_text("print('hi')\n")
    for relative, content in (extra or {}).items():
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return folder


def link(target, where):
    try:
        os.symlink(target, where)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없습니다.")


def sha(root, name="todo"):
    return LocalFolderSource(root).fetch(f"owner/{name}").commit_sha


# --- 정상 ---------------------------------------------------------------------------------
def test_a_complete_folder_gives_its_path_a_commit_sha_and_the_spec(tmp_path):
    folder = make(tmp_path)
    result = LocalFolderSource(tmp_path).fetch("owner/todo")
    assert result.path == folder.resolve() and re.fullmatch(r"[0-9a-f]{12}", result.commit_sha)
    assert result.spec == {"app": "todo", "port": 8080}


def test_the_owner_part_is_ignored_and_a_bare_name_works(tmp_path):
    make(tmp_path)
    assert sha(tmp_path) == LocalFolderSource(tmp_path).fetch("todo").commit_sha


# --- SHA ----------------------------------------------------------------------------------
def test_the_same_content_gives_the_same_sha_even_in_another_place(tmp_path):
    make(tmp_path / "one")
    shutil.copytree(tmp_path / "one" / "todo", tmp_path / "two" / "todo")
    assert sha(tmp_path / "one") == sha(tmp_path / "two") == sha(tmp_path / "one")


@pytest.mark.parametrize(
    "change",
    [
        lambda f: (f / "app" / "main.py").write_text("print('changed')\n"),
        lambda f: (f / "app" / "extra.py").write_text(""),
        lambda f: (f / "app" / "main.py").unlink(),
        lambda f: (f / "app" / "main.py").rename(f / "app" / "renamed.py"),
        lambda f: (f / "Dockerfile").write_text("FROM python:3.13-slim\n"),
        lambda f: (f / "deploy-spec.yaml").write_text("app: todo\nport: 9090\n"),
    ],
    ids=["edit", "add", "delete", "rename", "dockerfile", "spec"],
)
def test_any_change_that_reaches_the_image_changes_the_sha(tmp_path, change):
    folder = make(tmp_path)
    before = sha(tmp_path)
    change(folder)
    assert sha(tmp_path) != before


def test_the_order_the_file_system_lists_files_in_does_not_change_the_sha(tmp_path, monkeypatch):
    make(tmp_path, extra={"app/a.py": "a", "app/b.py": "b", "z/c.py": "c"})
    before = sha(tmp_path)
    real_walk = os.walk

    def reversed_walk(*args, **kwargs):
        for directory, dirs, files in real_walk(*args, **kwargs):
            files.reverse()
            dirs.reverse()
            yield directory, dirs, files

    monkeypatch.setattr(os, "walk", reversed_walk)
    assert sha(tmp_path) == before


@pytest.mark.skipif(os.name == "nt", reason="Windows에서는 실행 권한 비트가 의미 없다")
def test_the_executable_bit_changes_the_sha(tmp_path):
    folder = make(tmp_path)
    before = sha(tmp_path)
    (folder / "app" / "main.py").chmod(0o755)
    assert sha(tmp_path) != before


def test_a_changed_link_target_changes_the_sha_without_following_it(tmp_path):
    folder = make(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("one")
    link(outside, folder / "app" / "ref")
    before = sha(tmp_path)
    outside.write_text("two")  # 링크가 가리키는 내용은 해시에 들어가지 않는다
    assert sha(tmp_path) == before
    (folder / "app" / "ref").unlink()
    link(tmp_path / "other.txt", folder / "app" / "ref")  # 가리키는 곳이 바뀌면 달라진다
    assert sha(tmp_path) != before


@pytest.mark.parametrize(
    "ignored",
    [
        ".git/config",
        ".git/objects/ab/cd",
        ".env",
        ".env.production",
        "app/.env",
        ".envs/secret",
        "app/__pycache__/main.pyc",
    ],
)
def test_files_the_builder_does_not_copy_do_not_change_the_sha(tmp_path, ignored):
    folder = make(tmp_path)
    before = sha(tmp_path)
    target = folder / ignored
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("anything")
    assert sha(tmp_path) == before


# --- 이름과 위치 --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "owner/..",
        "owner/.",
        "owner/",
        "",
        "owner/a b",
        "owner/a;b",
        "owner/.hidden",
        "owner/a\\b",
        "owner/" + "a" * 101,
    ],
)
def test_a_repository_name_that_is_not_a_safe_folder_name_is_refused(tmp_path, name):
    make(tmp_path)
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch(name)
    assert caught.value.code == "source_not_found"


@pytest.mark.parametrize("name", ["a b", "a;b", ".hidden"])
def test_an_unsafe_name_is_refused_even_when_such_a_folder_exists(tmp_path, name):
    make(tmp_path, name=name)  # 폴더가 실제로 있어도 이름 규칙이 먼저 거절해야 한다
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch(f"owner/{name}")
    assert caught.value.code == "source_not_found"


def test_the_parent_of_the_root_is_never_a_source(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "deploy-spec.yaml").write_text(SPEC)
    root = tmp_path / "root"
    root.mkdir()
    for name in ("owner/..", "owner/."):
        with pytest.raises(SourceError) as caught:
            LocalFolderSource(root).fetch(name)
        assert caught.value.code == "source_not_found"


def test_a_missing_folder_or_a_plain_file_is_not_found(tmp_path):
    (tmp_path / "file").write_text("x")
    for name in ("owner/missing", "owner/file"):
        with pytest.raises(SourceError) as caught:
            LocalFolderSource(tmp_path).fetch(name)
        assert caught.value.code == "source_not_found"


def test_a_folder_that_is_a_link_to_somewhere_else_is_refused(tmp_path):
    outside = make(tmp_path / "outside")
    root = tmp_path / "root"
    root.mkdir()
    link(outside, root / "todo")
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(root).fetch("owner/todo")
    assert caught.value.code == "source_not_found"


# --- 필수 파일과 명세 ---------------------------------------------------------------------
@pytest.mark.parametrize("missing", ["Dockerfile", "deploy-spec.yaml"])
def test_a_missing_required_file_is_reported_before_anything_runs(tmp_path, missing):
    (make(tmp_path) / missing).unlink()
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch("owner/todo")
    assert caught.value.code == "source_incomplete" and missing in caught.value.message


def test_a_required_file_that_is_a_directory_or_a_link_is_refused(tmp_path):
    folder = make(tmp_path)
    (folder / "Dockerfile").unlink()
    (folder / "Dockerfile").mkdir()
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch("owner/todo")
    assert caught.value.code == "source_incomplete"
    (folder / "Dockerfile").rmdir()
    secret = tmp_path / "secret"
    secret.write_text("x")
    link(secret, folder / "Dockerfile")
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch("owner/todo")
    assert caught.value.code == "source_incomplete"


@pytest.mark.parametrize("spec", ["- a\n- b\n", "just text", "a: [unclosed", "", "app: \x00bad"])
def test_a_spec_that_is_not_a_mapping_or_not_yaml_is_refused(tmp_path, spec):
    make(tmp_path, spec=spec)
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch("owner/todo")
    assert caught.value.code == "source_incomplete"


def test_a_spec_cannot_construct_python_objects(tmp_path):
    make(tmp_path, spec="app: !!python/object/apply:os.getcwd []\n")
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch("owner/todo")
    assert caught.value.code == "source_incomplete"


def test_an_oversized_spec_is_refused(tmp_path):
    make(tmp_path, spec="app: todo\n" + "# " + "x" * source.MAX_SPEC_BYTES + "\n")
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch("owner/todo")
    assert caught.value.code == "source_incomplete"


# --- 제한 ---------------------------------------------------------------------------------
def test_a_source_over_the_size_limit_is_refused(tmp_path, monkeypatch):
    make(tmp_path)
    monkeypatch.setattr(source, "MAX_BYTES", 10)
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch("owner/todo")
    assert caught.value.code == "source_too_large"


def test_a_source_with_too_many_files_is_refused(tmp_path, monkeypatch):
    make(tmp_path)
    monkeypatch.setattr(source, "MAX_ENTRIES", 2)
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch("owner/todo")
    assert caught.value.code == "source_too_large"


def test_files_the_builder_skips_do_not_count_against_the_limits(tmp_path, monkeypatch):
    folder = make(tmp_path)
    (folder / ".env").write_text("x" * 1000)
    (folder / ".git").mkdir()
    (folder / ".git" / "pack").write_text("x" * 1000)
    monkeypatch.setattr(source, "MAX_BYTES", 500)
    assert LocalFolderSource(tmp_path).fetch("owner/todo")


# --- 비밀과 읽기 전용 ---------------------------------------------------------------------
def test_error_messages_do_not_contain_server_paths(tmp_path):
    messages = []
    for name in ("owner/missing", "owner/.."):
        with pytest.raises(SourceError) as caught:
            LocalFolderSource(tmp_path).fetch(name)
        messages.append(caught.value.message)
    (make(tmp_path) / "Dockerfile").unlink()
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch("owner/todo")
    messages.append(caught.value.message)
    assert all(
        str(tmp_path) not in message and tmp_path.name not in message for message in messages
    )


def test_the_source_folder_is_only_read(tmp_path):
    folder = make(tmp_path, extra={".env": "SECRET=1\n"})

    def snapshot():
        return {
            str(p.relative_to(folder)): (
                p.stat().st_mtime_ns,
                p.read_bytes() if p.is_file() else None,
            )
            for p in sorted(folder.rglob("*"))
        }

    before = snapshot()
    LocalFolderSource(tmp_path).fetch("owner/todo")
    assert snapshot() == before


# --- GitHub에서 받기 ---------------------------------------------------------------------
COMMIT = "0123456789abcdef0123456789abcdef01234567"
TOKEN = "gho_" + "x" * 30


class FakeClone:
    """clone_repository를 흉내 낸다. 받은 인자를 기록하고 destination에 파일을 만든다."""

    def __init__(self, files=None, error=None):
        self.calls, self.error = [], error
        self.files = (
            {
                "Dockerfile": "FROM python:3.12-slim\n",
                "deploy-spec.yaml": SPEC,
                "app/main.py": "print('hi')\n",
            }
            if files is None
            else files
        )

    def __call__(self, repo, branch, destination, token, depth=None):
        self.calls.append(
            dict(repo=repo, branch=branch, destination=destination, token=token, depth=depth)
        )
        if self.error:
            raise self.error
        for relative, content in self.files.items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return destination


def github(tmp_path, clone=None, revision=lambda folder: COMMIT):
    clone = clone or FakeClone()
    return GitHubCloneSource(tmp_path / "work", clone=clone, revision=revision), clone


def fetch(provider, repo="owner/todo-app2", branch="main", token=TOKEN):
    return provider.fetch(repo, branch, token)


def leftovers(tmp_path):
    work = tmp_path / "work"
    return sorted(p.name for p in work.iterdir()) if work.is_dir() else []


def test_the_branch_is_cloned_shallowly_with_the_users_token_and_the_commit_becomes_the_tag(
    tmp_path,
):
    provider, clone = github(tmp_path)
    result = fetch(provider)
    (call,) = clone.calls
    assert (call["repo"], call["branch"], call["token"], call["depth"]) == (
        "owner/todo-app2",
        "main",
        TOKEN,
        1,
    )
    assert result.commit_sha == COMMIT[:12] and re.fullmatch(r"[0-9a-f]{12}", result.commit_sha)
    assert result.spec == {"app": "todo", "port": 8080} and result.path == call["destination"]
    assert result.path.is_relative_to((tmp_path / "work").resolve()) or str(result.path).startswith(
        str(tmp_path / "work")
    )


def test_cleanup_removes_the_received_source_and_leaves_the_workspace(tmp_path):
    provider, _ = github(tmp_path)
    result = fetch(provider)
    assert result.path.is_dir() and len(leftovers(tmp_path)) == 1
    result.cleanup()
    assert leftovers(tmp_path) == [] and (tmp_path / "work").is_dir()
    result.cleanup()  # 두 번 불러도 안전하다


def test_two_fetches_get_separate_folders_and_cleaning_one_keeps_the_other(tmp_path):
    provider, _ = github(tmp_path)
    first, second = fetch(provider), fetch(provider)
    assert first.path != second.path
    first.cleanup()
    assert not first.path.exists() and second.path.is_dir()


def test_the_token_is_not_part_of_the_result(tmp_path):
    provider, _ = github(tmp_path)
    result = fetch(provider)
    assert TOKEN not in repr(result) and TOKEN not in str(result.spec)


@pytest.mark.parametrize(
    "repo",
    [
        "",
        "todo",
        "owner/",
        "/todo",
        "owner/..",
        "../todo",
        "owner/a b",
        "a/b/c",
        "owner/a;b",
        "owner/" + "a" * 101,
        "owner/todo\n",
    ],
)
def test_an_invalid_repository_name_is_refused_before_cloning(tmp_path, repo):
    provider, clone = github(tmp_path)
    with pytest.raises(SourceError) as caught:
        fetch(provider, repo=repo)
    assert (
        caught.value.code == "source_not_found" and clone.calls == [] and leftovers(tmp_path) == []
    )


@pytest.mark.parametrize(
    "branch", ["main", "feature/55/deployment", "release-1.2", "v1.0_rc", "a" * 255]
)
def test_ordinary_branch_names_are_accepted(tmp_path, branch):
    provider, clone = github(tmp_path)
    assert fetch(provider, branch=branch) and clone.calls[0]["branch"] == branch


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "-x",
        "--upload-pack=evil",
        "/x",
        "a..b",
        "a b",
        "x;rm",
        "a/",
        "x.lock",
        "feat\n",
        "a" * 256,
    ],
)
def test_a_branch_name_that_could_be_an_option_or_is_malformed_is_refused_before_cloning(
    tmp_path, branch
):
    provider, clone = github(tmp_path)
    with pytest.raises(SourceError) as caught:
        fetch(provider, branch=branch)
    assert (
        caught.value.code == "source_not_found" and clone.calls == [] and leftovers(tmp_path) == []
    )


@pytest.mark.parametrize("token", [None, ""])
def test_a_missing_token_is_reported_and_nothing_is_cloned(tmp_path, token):
    provider, clone = github(tmp_path)
    with pytest.raises(SourceError) as caught:
        fetch(provider, token=token)
    assert (
        caught.value.code == "github_token_missing"
        and clone.calls == []
        and leftovers(tmp_path) == []
    )


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(
            128,
            ["git", "clone"],
            stderr=f"fatal: could not read from https://x-access-token:{TOKEN}@github.com",
        ),
        subprocess.TimeoutExpired(["git", "clone"], 120),
        OSError(f"disk full {TOKEN}"),
    ],
)
def test_a_failed_clone_is_reported_without_gits_output_and_leaves_nothing_behind(tmp_path, error):
    provider, _ = github(tmp_path, FakeClone(error=error))
    with pytest.raises(SourceError) as caught:
        fetch(provider)
    assert (
        caught.value.code == "source_fetch_failed"
        and TOKEN not in caught.value.message
        and "github.com" not in caught.value.message
    )
    assert leftovers(tmp_path) == []


def test_an_unexpected_exception_still_cleans_up_and_is_not_swallowed(tmp_path):
    provider, _ = github(tmp_path, FakeClone(error=RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        fetch(provider)
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("revision", ["", "xyz", "0123456", None, COMMIT.upper(), COMMIT + "0"])
def test_a_revision_that_is_not_a_full_commit_hash_is_refused(tmp_path, revision):
    provider, _ = github(tmp_path, revision=lambda folder: revision)
    with pytest.raises(SourceError) as caught:
        fetch(provider)
    assert caught.value.code == "source_fetch_failed" and leftovers(tmp_path) == []


@pytest.mark.parametrize(
    "files, code",
    [
        ({"deploy-spec.yaml": SPEC}, "source_incomplete"),
        ({"Dockerfile": "FROM x\n"}, "source_incomplete"),
        ({"Dockerfile": "FROM x\n", "deploy-spec.yaml": "- a\n- b\n"}, "source_incomplete"),
        ({"Dockerfile": "FROM x\n", "deploy-spec.yaml": "a: [unclosed"}, "source_incomplete"),
    ],
)
def test_an_incomplete_received_source_is_refused_and_removed(tmp_path, files, code):
    provider, _ = github(tmp_path, FakeClone(files=files))
    with pytest.raises(SourceError) as caught:
        fetch(provider)
    assert caught.value.code == code and leftovers(tmp_path) == []


def test_an_oversized_received_source_is_refused_and_removed(tmp_path, monkeypatch):
    provider, _ = github(tmp_path)
    monkeypatch.setattr(source, "MAX_BYTES", 10)
    with pytest.raises(SourceError) as caught:
        fetch(provider)
    assert caught.value.code == "source_too_large" and leftovers(tmp_path) == []


def test_leftovers_of_a_crashed_process_are_cleared_without_touching_the_workspace(tmp_path):
    provider, _ = github(tmp_path)
    fetch(provider), fetch(provider)
    assert len(leftovers(tmp_path)) == 2
    provider.clear_stale()
    assert leftovers(tmp_path) == [] and (tmp_path / "work").is_dir()
    GitHubCloneSource(
        tmp_path / "never-created"
    ).clear_stale()  # 작업 폴더가 아직 없어도 오류가 아니다


@pytest.mark.skipif(shutil.which("git") is None, reason="git이 필요하다")
def test_the_default_revision_reads_the_real_head_commit_of_the_cloned_folder(tmp_path):
    def clone_with_git(repo, branch, destination, token, depth=None):
        destination.mkdir(parents=True)
        for name, content in {"Dockerfile": "FROM x\n", "deploy-spec.yaml": SPEC}.items():
            (destination / name).write_text(content)
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
        subprocess.run(["git", *identity, "add", "."], cwd=destination, check=True)
        subprocess.run(
            ["git", *identity, "commit", "-q", "-m", "init"], cwd=destination, check=True
        )
        return destination

    provider = GitHubCloneSource(tmp_path / "work", clone=clone_with_git)
    result = provider.fetch("owner/todo-app2", "main", TOKEN)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=result.path, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert result.commit_sha == head[:12] and (result.path / ".git").is_dir()


@pytest.mark.parametrize("name", ["owner/todo\n", "todo\n"])
def test_a_trailing_newline_does_not_slip_through_the_name_check_of_the_local_folder_either(
    tmp_path, name
):
    make(tmp_path)  # "todo" 폴더는 있다. 이름 끝의 줄바꿈은 다른 이름이다
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch(name)
    assert caught.value.code == "source_not_found"


def test_a_folder_whose_name_ends_with_a_newline_is_never_taken_for_the_plain_name(tmp_path):
    try:
        make(tmp_path, name="todo\n")
    except OSError:
        pytest.skip("이 파일 시스템에서는 이름에 줄바꿈이 든 폴더를 만들 수 없다")
    with pytest.raises(SourceError) as caught:
        LocalFolderSource(tmp_path).fetch("owner/todo\n")
    assert caught.value.code == "source_not_found"

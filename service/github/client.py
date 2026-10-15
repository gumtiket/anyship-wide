from pathlib import Path

from . import changes, pull_request, repository
from .errors import (
    AuthenticationError,
    GitHubAPIError,
    InvalidRepositoryError,
    ValidationError,
    translate_errors,
)
from .models import CommitResult, PullRequest, PushResult, Workspace


class GitHubRepository:
    def __init__(self, url: str, path: str | Path, token: str) -> None:
        try:
            self.repository = repository.parse_repo_url(url)
        except ValueError:
            raise InvalidRepositoryError("Expected a GitHub HTTPS repository URL.") from None

        if not token.strip():
            raise AuthenticationError("A GitHub token is required.")

        self.path = Path(path).expanduser().resolve()
        self._token = token

    def get_default_branch(self) -> str:
        with translate_errors():
            branch = pull_request.get_default_branch(self.repository, self._token)
            if not isinstance(branch, str) or not branch:
                raise GitHubAPIError("GitHub returned an invalid default branch.")
            return branch

    def clone(self, branch: str | None = None) -> Path:
        if self.path.exists():
            raise InvalidRepositoryError("Clone destination already exists.")
        branch = branch or self.get_default_branch()

        with translate_errors():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return repository.clone_repository(self.repository, branch, self.path, self._token)

    def inspect(self, *, base_branch: str | None = None) -> Workspace:
        with translate_errors():
            self._validate_repository_root()
            self._validate_origin_urls()

            branch = repository.run_git(["branch", "--show-current"], cwd=self.path)
            if not branch or branch == base_branch:
                raise InvalidRepositoryError("An attached work branch is required.")
            return Workspace(
                repository=self.repository,
                path=self.path,
                branch=branch,
            )

    def create_branch(self, name: str) -> Workspace:
        self.inspect()
        if not name or name.startswith("-"):
            raise InvalidRepositoryError("Invalid branch name.")

        with translate_errors():
            repository.run_git(["check-ref-format", "--branch", name], cwd=self.path)
            repository.create_branch(self.path, name)
        return Workspace(repository=self.repository, path=self.path, branch=name)

    def get_changes(self) -> str:
        self.inspect()

        with translate_errors():
            return changes.get_changes(self.path)

    def get_diff(self) -> str:
        self.inspect()

        with translate_errors():
            return changes.get_diff(self.path)

    def commit(
        self,
        message: str,
        *,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> CommitResult:
        if not message.strip():
            raise ValidationError("Commit message is required.")
        self.inspect()

        with translate_errors():
            diff_summary = changes.stage_changes(self.path)
            if not diff_summary:
                return CommitResult(created=False, sha=None, summary="")
            changes.commit_changes(
                self.path,
                message,
                author_name=author_name,
                author_email=author_email,
            )
            sha = repository.run_git(["rev-parse", "HEAD"], cwd=self.path)
            return CommitResult(created=True, sha=sha, summary=diff_summary)

    def push(self) -> PushResult:
        base_branch = self.get_default_branch()
        workspace = self.inspect(base_branch=base_branch)

        with translate_errors():
            changes.push_branch(self.path, workspace.branch, self._token)
        return PushResult(repository=self.repository, branch=workspace.branch)

    def create_pull_request(
        self,
        title: str,
        *,
        body: str = "",
        base: str | None = None,
        draft: bool = True,
    ) -> PullRequest:
        if not title.strip():
            raise ValidationError("Pull request title is required.")

        base_branch = base or self.get_default_branch()
        workspace = self.inspect(base_branch=base_branch)

        with translate_errors():
            data = pull_request.create_pull_request(
                self.repository,
                workspace.branch,
                base_branch,
                self._token,
                title=title,
                body=body,
                draft=draft,
            )
            number = data["number"]
            url = data["url"]
            if type(number) is not int or not isinstance(url, str):
                raise GitHubAPIError("GitHub returned an invalid pull request.")
            return PullRequest(number=number, url=url)

    def _validate_repository_root(self) -> None:
        root = repository.run_git(["rev-parse", "--show-toplevel"], cwd=self.path)
        if Path(root).resolve() != self.path:
            raise InvalidRepositoryError("Path must be the repository root.")

    def _validate_origin_urls(self) -> None:
        commands = (
            ["remote", "get-url", "--all", "origin"],
            ["remote", "get-url", "--push", "--all", "origin"],
        )
        for command in commands:
            remote_urls = repository.run_git(command, cwd=self.path).splitlines()
            if not remote_urls:
                raise InvalidRepositoryError("Origin URL is missing.")

            for remote_url in remote_urls:
                try:
                    remote_repository = repository.parse_repo_url(remote_url)
                except ValueError:
                    raise InvalidRepositoryError("Origin must use GitHub HTTPS.") from None

                if remote_repository.lower() != self.repository.lower():
                    raise InvalidRepositoryError("Origin does not match the requested repository.")

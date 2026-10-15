import subprocess
from collections.abc import Iterator
from contextlib import contextmanager

import requests


class GitHubError(Exception):
    pass


class ValidationError(GitHubError, ValueError):
    pass


class InvalidRepositoryError(ValidationError):
    pass


class AuthenticationError(GitHubError):
    pass


class GitCommandError(GitHubError):
    pass


class GitHubAPIError(GitHubError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@contextmanager
def translate_errors() -> Iterator[None]:
    try:
        yield
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            raise AuthenticationError(
                "GitHub access denied; check token, permissions and policy."
            ) from None
        raise GitHubAPIError("GitHub request failed.", status) from None
    except requests.RequestException:
        raise GitHubAPIError("GitHub connection or response failed.") from None
    except (subprocess.SubprocessError, OSError):
        raise GitCommandError(
            "Git operation failed; check Git, path, credentials and branch state."
        ) from None
    except ValidationError:
        raise
    except (ValueError, KeyError, TypeError):
        raise GitHubAPIError("GitHub returned an invalid response.") from None

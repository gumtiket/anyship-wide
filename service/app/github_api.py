from urllib.parse import quote

import httpx


class GitHubFailure(Exception):
    def __init__(self, status=502, code="github_error"):
        self.status = status
        # Never retain GitHub error descriptions, URLs, tokens or arbitrary payloads.
        allowed = {
            "github_error",
            "network_error",
            "invalid_response",
            "token_exchange_failed",
            "incorrect_client_credentials",
            "redirect_uri_mismatch",
            "bad_verification_code",
            "incorrect_code_verifier",
            "access_denied",
        }
        self.code = code if code in allowed else "github_error"


class GitHubAPI:
    """Uses a user token: access is the intersection of App and user permissions."""

    def _request(self, method, url, **kwargs):
        try:
            response = httpx.request(method, url, timeout=20, follow_redirects=False, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            raise GitHubFailure(
                status if status in (401, 403, 404, 409, 422, 429) else 502
            ) from None
        except httpx.HTTPError:
            raise GitHubFailure(code="network_error") from None
        except ValueError:
            raise GitHubFailure(code="invalid_response") from None

    def exchange(self, settings, code, verifier):
        data = self._request(
            "POST",
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": settings.app_origin + "/api/auth/github/callback",
            },
        )
        if not data.get("access_token"):
            raise GitHubFailure(401, data.get("error", "token_exchange_failed"))
        return data

    def get(self, token, path, params=None):
        return self._request(
            "GET",
            "https://api.github.com" + path,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def post(self, token, path, body):
        return self._request(
            "POST",
            "https://api.github.com" + path,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def pages(self, token, path, key=None):
        result = []
        for page in range(1, 101):
            data = self.get(token, path, {"per_page": 100, "page": page})
            items = data[key] if key else data
            result.extend(items)
            if len(items) < 100:
                return result
        raise GitHubFailure()  # Never silently return a truncated authorization list.

    def profile(self, token):
        return self.get(token, "/user")

    def installations(self, token):
        return self.pages(token, "/user/installations", "installations")

    def repositories(self, token, installation):
        return self.pages(token, f"/user/installations/{installation}/repositories", "repositories")

    def branches(self, token, name):
        return self.pages(token, f"/repos/{quote(name, safe='/')}/branches")

    def branch(self, token, name, branch):
        return self.get(token, f"/repos/{quote(name, safe='/')}/branches/{quote(branch, safe='')}")

    def git_commit(self, token, name, sha):
        return self.get(token, f"/repos/{name}/git/commits/{quote(sha, safe='')}")

    def tree(self, token, name, sha):
        data = self.get(token, f"/repos/{name}/git/trees/{quote(sha, safe='')}")
        if data.get("truncated"):
            raise GitHubFailure(409)
        return data

    def create_tree(self, token, name, base_tree, filename, content):
        return self.post(
            token,
            f"/repos/{name}/git/trees",
            {
                "base_tree": base_tree,
                "tree": [{"path": filename, "mode": "100644", "type": "blob", "content": content}],
            },
        )["sha"]

    def recursive_tree(self, token, name, sha):
        return self.get(token, f"/repos/{name}/git/trees/{quote(sha, safe='')}", {"recursive": "1"})

    def blob(self, token, name, sha):
        return self.get(token, f"/repos/{name}/git/blobs/{quote(sha, safe='')}")

    def create_files_tree(self, token, name, base_tree, files):
        return self.post(
            token,
            f"/repos/{name}/git/trees",
            {
                "base_tree": base_tree,
                "tree": [
                    {"path": f["path"], "mode": f["mode"], "type": "blob", "content": f["content"]}
                    for f in files
                ],
            },
        )["sha"]

    def create_commit(self, token, name, base_sha, tree_sha, message):
        return self.post(
            token,
            f"/repos/{name}/git/commits",
            {
                "message": message,
                "tree": tree_sha,
                "parents": [base_sha],
            },
        )["sha"]

    def ref(self, token, name, branch):
        try:
            return self.get(token, f"/repos/{name}/git/ref/heads/{quote(branch, safe='')}")[
                "object"
            ]["sha"]
        except GitHubFailure as error:
            if error.status == 404:
                return None
            raise

    def create_ref(self, token, name, branch, sha):
        return self.post(
            token, f"/repos/{name}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}
        )

    def find_pr(self, token, name, branch, base):
        data = self.get(
            token,
            f"/repos/{name}/pulls",
            {
                "state": "all",
                "head": f"{name.split('/')[0]}:{branch}",
                "base": base,
                "per_page": 100,
            },
        )
        return next(
            (pr for pr in data if pr["head"]["ref"] == branch and pr["base"]["ref"] == base), None
        )

    def create_pr(self, token, name, branch, base, title, body):
        return self.post(
            token,
            f"/repos/{name}/pulls",
            {
                "title": title,
                "body": body,
                "head": branch,
                "base": base,
                "draft": True,
            },
        )


class DemoGitHub:
    """Explicit offline fixture; never accesses GitHub or writes remote resources."""

    def installations(self, token):
        return [{"id": 101, "account": {"login": "demo-workspace"}, "app_slug": "demo-workspace"}]

    def repositories(self, token, installation):
        return [
            {
                "id": 1001,
                "full_name": "demo-workspace/todo-api",
                "description": "FastAPI로 만든 할 일 서비스",
                "private": True,
                "default_branch": "main",
                "language": "Python",
                "permissions": {"push": True},
                "archived": False,
            },
            {
                "id": 1002,
                "full_name": "demo-workspace/team-notes",
                "description": "팀의 노트를 관리하는 API",
                "private": False,
                "default_branch": "main",
                "language": "Python",
                "permissions": {"push": True},
                "archived": False,
            },
        ]

    def branches(self, token, name):
        return [
            {"name": "main", "commit": {"sha": "a" * 40}},
            {"name": "develop", "commit": {"sha": "b" * 40}},
        ]

    def branch(self, token, name, branch):
        for item in self.branches(token, name):
            if item["name"] == branch:
                return item
        raise GitHubFailure(404)

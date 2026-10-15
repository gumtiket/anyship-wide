"""로그인 사용자가 GitHub 저장소와 프로젝트에 접근할 수 있는지 확인한다."""

from fastapi import HTTPException
from sqlalchemy import select

from . import onboarding
from .db import Project


class ProjectAccess:
    def __init__(self, settings, github, access_token):
        self.settings = settings
        self.github = github
        self.access_token = access_token

    def installations(self, login):
        return self.github.installations(self.access_token(login))

    def repositories(self, login, installation):
        if not any(item["id"] == installation for item in self.installations(login)):
            raise HTTPException(404, "접근할 수 없는 GitHub 연결입니다.")
        return self.github.repositories(self.access_token(login), installation)

    def repository(self, login, installation, repository_id):
        """쓰기 권한이 있는 활성 저장소만 돌려준다."""
        for item in self.repositories(login, installation):
            if item["id"] == repository_id:
                if item.get("archived") or not item.get("permissions", {}).get("push"):
                    raise HTTPException(
                        403, "프로젝트 등록에는 활성 저장소의 쓰기 권한이 필요합니다."
                    )
                return item
        raise HTTPException(404, "접근할 수 없는 저장소입니다.")

    def resolve_repository(self, login, url):
        """저장소 URL을 (설치 ID, 저장소 정보)로 바꾼다."""
        result = onboarding.inspect_access(
            self.github,
            self.access_token(login),
            onboarding.repository_name(url),
            self.settings.demo,
        )
        if result["status"] != "ready":
            raise HTTPException(
                404 if result["status"] == "authorization_required" else 403, result["message"]
            )
        return result["installation_id"], result["repository"]

    def owned_project(self, project_id, login, session):
        """로그인 사용자의 워크스페이스에 속한 프로젝트를 돌려준다."""
        row = session.scalar(
            select(Project).where(
                Project.id == project_id, Project.workspace_id == login.workspace_id
            )
        )
        if not row:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        return row

    def real_project(self, project_id, login, session):
        """실제 GitHub에 쓸 수 있는 프로젝트인지(저장소 주소, App 설치·권한) 확인한다."""
        if self.settings.demo:
            raise HTTPException(409, "실제 GitHub 로그인 모드로 실행해 주세요.")
        project = self.owned_project(project_id, login, session)
        repo = self.repository(login, project.installation_id, project.repository_id)
        if repo["full_name"].lower() != project.full_name.lower():
            raise HTTPException(
                409, "저장소 주소가 변경되었습니다. 프로젝트 연결 정보를 확인해 주세요."
            )
        installation = next(
            (i for i in self.installations(login) if i["id"] == project.installation_id), None
        )
        if not installation:
            raise HTTPException(404, "GitHub App 연결이 해제되었습니다.")
        permissions = installation.get("permissions", {})
        if installation.get("suspended_at") or any(
            permissions.get(key) != "write" for key in ("contents", "pull_requests")
        ):
            raise HTTPException(
                403,
                "GitHub App의 Contents와 Pull requests를 Read and write로 설정하고 설치 권한을 승인해 주세요.",
            )
        return project

"""GitHub 설치·저장소 조회와 프로젝트(저장소 연결) 등록·조회·삭제 경로."""

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from . import analyses, deployments, mock_deployments
from .db import CodeChange, DemoChange, Project, RepositoryConnection


class ProjectInput(BaseModel):
    repository_url: str = Field(default="", max_length=2048)
    installation_id: int | None = Field(default=None, gt=0)
    repository_id: int | None = Field(default=None, gt=0)
    branch: str = Field(min_length=1, max_length=255)


class RepositoryInput(BaseModel):
    repository_url: str = Field(min_length=1, max_length=2048)


def project_json(item):
    return {
        "id": item.id,
        "full_name": item.full_name,
        "branch": item.branch,
        "base_sha": item.base_sha,
        "created_at": item.created_at,
        "status": "connected",
    }


def router(github, access, db, current, mutation, access_token):
    routes = APIRouter(prefix="/api")

    @routes.get("/github/install")
    def install(login=Depends(current)):
        return RedirectResponse("/#connect", status_code=302)

    @routes.get("/github/installations")
    def installation_list(login=Depends(current)):
        return [
            {"id": item["id"], "account": item["account"]["login"]}
            for item in access.installations(login)
        ]

    @routes.post("/github/resolve")
    def resolve(body: RepositoryInput, login=Depends(mutation)):
        installation_id, repo = access.resolve_repository(login, body.repository_url)
        return {
            "installation_id": installation_id,
            "id": repo["id"],
            "full_name": repo["full_name"],
            "private": repo["private"],
            "default_branch": repo["default_branch"],
            "branches": [
                {"name": b["name"], "sha": b["commit"]["sha"]}
                for b in github.branches(access_token(login), repo["full_name"])
            ],
        }

    @routes.get("/github/installations/{installation_id}/repositories")
    def repository_list(installation_id: int, login=Depends(current)):
        return [
            {
                "id": item["id"],
                "full_name": item["full_name"],
                "description": item.get("description"),
                "private": item["private"],
                "default_branch": item["default_branch"],
                "language": item.get("language"),
                "can_write": bool(item.get("permissions", {}).get("push"))
                and not item.get("archived", False),
            }
            for item in access.repositories(login, installation_id)
        ]

    @routes.get("/github/installations/{installation_id}/repositories/{repository_id}/branches")
    def branch_list(installation_id: int, repository_id: int, login=Depends(current)):
        repo = access.repository(login, installation_id, repository_id)
        return [
            {"name": item["name"], "sha": item["commit"]["sha"]}
            for item in github.branches(access_token(login), repo["full_name"])
        ]

    @routes.get("/projects")
    def projects(login=Depends(current), session=Depends(db)):
        rows = session.scalars(
            select(Project)
            .where(Project.workspace_id == login.workspace_id)
            .order_by(Project.created_at.desc())
        )
        return [project_json(row) for row in rows]

    @routes.get("/projects/{project_id}")
    def project_detail(project_id: str, login=Depends(current), session=Depends(db)):
        return project_json(access.owned_project(project_id, login, session))

    @routes.delete("/projects/{project_id}", status_code=204)
    def delete_project(project_id: str, login=Depends(mutation), session=Depends(db)):
        project = session.scalar(
            select(Project)
            .where(
                Project.id == project_id,
                Project.workspace_id == login.workspace_id,
            )
            .with_for_update()
        )
        if project is None:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        mock_deployments.clear_targets(session, project_id=project_id)
        deployments.clear_real_targets(session, project_id=project_id)
        analyses.clear_project(session, project_id)
        # 조건부 삭제는 같은 행을 선점하려는 작업과 경쟁한다. 모든 변경을 한 트랜잭션에 묶어
        # 처리 중인 하위 작업이 있으면 정리 전체가 롤백되게 한다.
        session.execute(
            delete(CodeChange).where(
                CodeChange.project_id == project_id,
                CodeChange.lease_until <= int(time.time()),
            )
        )
        session.execute(
            delete(DemoChange).where(
                DemoChange.project_id == project_id,
                DemoChange.status.notin_(("applying", "publishing")),
            )
        )
        if session.scalar(
            select(CodeChange.id).where(CodeChange.project_id == project_id)
        ) or session.scalar(select(DemoChange.id).where(DemoChange.project_id == project_id)):
            raise HTTPException(409, "작업 처리 중입니다. 완료 후 저장소 연결을 삭제해 주세요.")
        session.delete(project)
        session.commit()
        return Response(status_code=204)

    @routes.post("/projects", status_code=201)
    def add_project(body: ProjectInput, login=Depends(mutation), session=Depends(db)):
        if body.repository_url:
            installation_id, repo = access.resolve_repository(login, body.repository_url)
        elif body.installation_id and body.repository_id:
            installation_id = body.installation_id
            repo = access.repository(login, installation_id, body.repository_id)
        else:
            raise HTTPException(422, "GitHub 저장소 URL을 입력해 주세요.")
        branch = github.branch(access_token(login), repo["full_name"], body.branch)
        row = Project(
            id=str(uuid.uuid4()),
            workspace_id=login.workspace_id,
            repository_id=repo["id"],
            installation_id=installation_id,
            full_name=repo["full_name"],
            branch=branch["name"],
            base_sha=branch["commit"]["sha"],
            created_by=login.user_id,
            created_at=int(time.time()),
        )
        session.add(row)
        try:
            # 등록을 마친 저장소의 연결 진행 기록(온보딩 초안)은 지운다.
            session.execute(
                delete(RepositoryConnection).where(
                    RepositoryConnection.user_id == login.user_id,
                    RepositoryConnection.workspace_id == login.workspace_id,
                    func.lower(RepositoryConnection.repository_url)
                    == f"https://github.com/{repo['full_name']}".lower(),
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(
                409, "이미 연결한 저장소입니다. 프로젝트 목록에서 확인해 주세요."
            ) from None
        return project_json(row)

    return routes

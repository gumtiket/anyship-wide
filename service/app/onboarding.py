"""사용자 소유 저장소 연결 초안. GitHub 리디렉트만으로는 접근 권한이 생기지 않는다."""

import hashlib
import secrets
import time
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from github.repository import parse_repo_url

from .db import Project, RepositoryConnection


def repository_name(url):
    try:
        return parse_repo_url(url.strip())
    except ValueError:
        raise HTTPException(
            422, "https://github.com/소유자/저장소 형식의 URL을 입력해 주세요."
        ) from None


def inspect_access(github, token, name, demo=False):
    """이 사용자의 App 토큰으로 확인할 수 있는 사실(권한 철회 포함)만 알려 준다."""
    owner = name.split("/")[0].lower()
    matching = [i for i in github.installations(token) if i["account"]["login"].lower() == owner]
    if matching and all(i.get("suspended_at") for i in matching):
        return {
            "status": "suspended",
            "message": "이 계정의 AnyShip 접근이 일시 중지되어 있습니다. 저장소 소유자 또는 조직 관리자에게 해제를 요청해 주세요.",
        }
    for installation in matching:
        if installation.get("suspended_at"):
            continue
        for repo in github.repositories(token, installation["id"]):
            if repo["full_name"].lower() != name.lower():
                continue
            if repo.get("archived"):
                return {
                    "status": "archived",
                    "message": "보관된 저장소에는 변경을 제출할 수 없습니다. 보관을 해제하거나 다른 저장소를 입력해 주세요.",
                }
            if not repo.get("permissions", {}).get("push"):
                return {
                    "status": "write_required",
                    "message": "이 저장소의 코드를 수정할 권한이 없습니다. 저장소 관리자에게 쓰기 권한을 요청해 주세요.",
                }
            permissions = installation.get("permissions", {})
            if not demo and any(
                permissions.get(key) != "write" for key in ("contents", "pull_requests")
            ):
                return {
                    "status": "permissions_required",
                    "message": "AnyShip에 코드 변경과 PR 생성 권한 승인이 필요합니다. GitHub에서 요청된 권한을 확인해 주세요. 권한 요청이 보이지 않으면 서비스 관리자에게 문의해 주세요.",
                }
            return {"status": "ready", "installation_id": installation["id"], "repository": repo}
    # GitHub는 접근할 수 없는 비공개 저장소를 숨긴다. 존재하지 않는다고 단정하지 않는다.
    return {
        "status": "authorization_required",
        "message": "이 저장소에 접근할 수 없습니다. 주소가 맞는지 확인한 뒤 GitHub에서 이 저장소의 접근을 허용해 주세요. 조직 저장소는 관리자 승인이 필요할 수 있습니다.",
    }


class ConnectionInput(BaseModel):
    repository_url: str = Field(min_length=1, max_length=2048)


class ConnectionRef(BaseModel):
    connection_id: str = Field(min_length=1, max_length=36)


class InstallationReturn(BaseModel):
    state: str = Field(min_length=1, max_length=256)


def router(settings, github, db, current, mutation, access_token):
    routes = APIRouter(prefix="/api/github/connection")

    def owned(login, session):
        return session.scalar(
            select(RepositoryConnection).where(
                RepositoryConnection.user_id == login.user_id,
                RepositoryConnection.workspace_id == login.workspace_id,
                RepositoryConnection.expires_at > int(time.time()),
            )
        )

    def require(login, session, identifier):
        draft = owned(login, session)
        if not draft or draft.id != identifier:
            raise HTTPException(
                409,
                "연결 요청이 만료되었거나 다른 창에서 변경되었습니다. 저장소 주소를 다시 확인해 주세요.",
            )
        return draft

    def summary(draft):
        return {
            "id": draft.id,
            "repository_url": draft.repository_url,
            "awaiting_approval": bool(draft.install_state_hash),
        }

    def inspect(draft, login, session):
        token = access_token(login)
        result = inspect_access(github, token, repository_name(draft.repository_url), settings.demo)
        if result["status"] == "ready":
            repo = result["repository"]
            project = session.scalar(
                select(Project).where(
                    Project.workspace_id == login.workspace_id, Project.repository_id == repo["id"]
                )
            )
            branches = (
                []
                if project
                else [
                    {"name": b["name"], "sha": b["commit"]["sha"]}
                    for b in github.branches(token, repo["full_name"])
                ]
            )
            result = {
                "status": "connected" if project else "ready" if branches else "empty_repository",
                "repository": {
                    "id": repo["id"],
                    "full_name": repo["full_name"],
                    "private": repo["private"],
                    "default_branch": repo["default_branch"],
                    "branches": branches,
                },
                "project_id": project.id if project else None,
            }
            if not project and not branches:
                result["message"] = (
                    "아직 커밋이 없는 저장소입니다. GitHub에서 README 등 첫 파일을 추가한 뒤 다시 확인해 주세요."
                )
        return {**summary(draft), **result}

    @routes.get("")
    def pending(login=Depends(current), session=Depends(db)):
        draft = owned(login, session)
        return summary(draft) if draft else None

    @routes.post("")
    def save(body: ConnectionInput, login=Depends(mutation), session=Depends(db)):
        name = repository_name(body.repository_url)
        # 같은 URL을 다시 제출해도 ID를 새로 만들고 이전 설치 상태를 무효화한다.
        session.execute(
            delete(RepositoryConnection).where(
                RepositoryConnection.user_id == login.user_id,
                RepositoryConnection.workspace_id == login.workspace_id,
            )
        )
        draft = RepositoryConnection(
            id=str(uuid.uuid4()),
            user_id=login.user_id,
            workspace_id=login.workspace_id,
            repository_url=f"https://github.com/{name}",
            expires_at=int(time.time()) + 86400,
            install_state_hash="",
            install_expires_at=0,
        )
        session.add(draft)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(
                409, "다른 창에서 연결 요청이 변경되었습니다. 다시 확인해 주세요."
            ) from None
        return summary(draft)

    @routes.post("/check")
    def check(body: ConnectionRef, login=Depends(mutation), session=Depends(db)):
        return inspect(require(login, session, body.connection_id), login, session)

    @routes.post("/authorize")
    def authorize(body: ConnectionRef, login=Depends(mutation), session=Depends(db)):
        draft = require(login, session, body.connection_id)
        if settings.demo or not settings.github_configured:
            raise HTTPException(
                503, "지금은 GitHub 연결을 시작할 수 없습니다. 잠시 후 다시 시도해 주세요."
            )
        state = secrets.token_urlsafe(32)
        draft.install_state_hash = hashlib.sha256(state.encode()).hexdigest()
        draft.install_expires_at = int(time.time()) + 3600
        session.commit()
        return {
            "url": f"https://github.com/apps/{settings.github_app_slug}/installations/new?"
            + urlencode({"state": state})
        }

    @routes.post("/return")
    def returned(body: InstallationReturn, login=Depends(mutation), session=Depends(db)):
        draft = owned(login, session)
        expected = hashlib.sha256(body.state.encode()).hexdigest()
        if (
            not draft
            or not draft.install_state_hash
            or draft.install_expires_at <= int(time.time())
            or not secrets.compare_digest(draft.install_state_hash, expected)
        ):
            raise HTTPException(
                409,
                "GitHub에서 돌아온 연결 요청을 확인할 수 없습니다. 저장된 주소로 접근 권한을 다시 확인해 주세요.",
            )
        consumed = session.execute(
            update(RepositoryConnection)
            .where(
                RepositoryConnection.id == draft.id,
                RepositoryConnection.install_state_hash == expected,
            )
            .values(install_state_hash="", install_expires_at=0)
        )
        session.commit()
        if consumed.rowcount != 1:
            raise HTTPException(409, "이미 확인한 연결 요청입니다. 접근 권한을 다시 확인해 주세요.")
        # 리디렉트로 받은 installation_id/setup_action을 접근 권한의 증거로 믿지 않는다.
        return inspect(draft, login, session)

    @routes.delete("")
    def cancel(body: ConnectionRef, login=Depends(mutation), session=Depends(db)):
        draft = require(login, session, body.connection_id)
        session.delete(draft)
        session.commit()
        return {"ok": True}

    return routes

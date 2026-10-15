"""AI 미연결(placeholder) 모드의 수정 항목 경로: 등록, 적용, Draft PR 게시."""

import secrets
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import code_changes
from .db import CodeChange
from .github_api import GitHubFailure

LEASE_SECONDS = 600


class AnalysisInput(BaseModel):
    restart: bool = False


class ReviewInput(BaseModel):
    review_hash: str = Field(min_length=64, max_length=64)


def change_json(row):
    if not row:
        return None
    return {
        "id": row.id,
        "status": row.status,
        "provider": "placeholder",
        "file": code_changes.FILE_NAME,
        "title": code_changes.TITLE,
        "content": row.content,
        "diff": row.diff,
        "review_hash": row.review_hash,
        "base_sha": row.base_sha,
        "branch": row.branch,
        "commit_sha": row.commit_sha,
        "pr_url": row.pr_url,
        "pr_number": row.pr_number,
        "busy": row.lease_until > int(time.time()),
        "error": row.error,
    }


def router(settings, github, access, db, current, mutation, access_token):
    routes = APIRouter(prefix="/api/projects/{project_id}")

    def require_placeholder():
        if settings.ai_mode != "placeholder":
            raise HTTPException(
                503,
                "AI 분석 모듈이 아직 연결되지 않았습니다. 테스트에는 APP_AI_MODE=placeholder 설정이 필요합니다.",
            )

    def get_change(project_id, session):
        row = session.scalar(select(CodeChange).where(CodeChange.project_id == project_id))
        if not row:
            raise HTTPException(409, "먼저 분석 요청으로 수정 항목을 등록해 주세요.")
        return row

    @routes.get("/changes")
    def changes(project_id: str, login=Depends(current), session=Depends(db)):
        access.owned_project(project_id, login, session)
        return change_json(
            session.scalar(select(CodeChange).where(CodeChange.project_id == project_id))
        )

    @routes.post("/analysis")
    def analyze(project_id: str, body: AnalysisInput, login=Depends(mutation), session=Depends(db)):
        require_placeholder()
        project = access.real_project(project_id, login, session)
        row = session.scalar(select(CodeChange).where(CodeChange.project_id == project_id))
        if row and (not body.restart or row.status == "pr_created"):
            return change_json(row)
        if row:
            # 같은 선점(lease)이 재등록·적용·게시가 서로 겹치지 않게 막는다.
            claimed = session.execute(
                update(CodeChange)
                .where(
                    CodeChange.id == row.id,
                    CodeChange.lease_until <= int(time.time()),
                    CodeChange.status != "pr_created",
                )
                .values(lease_until=int(time.time()) + LEASE_SECONDS)
            )
            session.commit()
            if claimed.rowcount != 1:
                raise HTTPException(409, "작업 처리 중입니다. 완료 후 다시 시도해 주세요.")
        try:
            if row and row.commit_sha:
                # 다시 시작하기 전에, 응답을 받지 못한 채 이미 만들어진 PR이 있으면 복구한다.
                existing = github.find_pr(
                    access_token(login), project.full_name, row.branch, project.branch
                )
                if existing:
                    code_changes.validate_pr(existing, project, row)
                    row.pr_url, row.pr_number, row.status = (
                        existing["html_url"],
                        existing["number"],
                        "pr_created",
                    )
                    row.lease_until = 0
                    session.commit()
                    return change_json(row)
            sha, tree = code_changes.snapshot(github, access_token(login), project)
        except Exception:
            if row:
                row.lease_until = 0
                session.commit()
            raise
        if row is None:
            row = CodeChange(id=str(uuid.uuid4()), project_id=project.id)
            session.add(row)
        row.base_sha, row.base_tree = sha, tree
        row.branch = "anyship/ai-placeholder-" + uuid.uuid4().hex[:16]
        row.content, row.status = code_changes.CONTENT, "proposed"
        row.diff = row.review_hash = row.tree_sha = row.commit_sha = row.error = ""
        row.lease_until = 0
        project.base_sha = sha
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            row = get_change(project_id, session)
        return change_json(row)

    @routes.post("/changes/apply")
    def apply_change(project_id: str, login=Depends(mutation), session=Depends(db)):
        require_placeholder()
        project = access.real_project(project_id, login, session)
        row = get_change(project_id, session)
        if row.status in ("applied", "pr_created"):
            return change_json(row)
        claimed = session.execute(
            update(CodeChange)
            .where(
                CodeChange.id == row.id,
                CodeChange.status == "proposed",
                CodeChange.lease_until <= int(time.time()),
            )
            .values(lease_until=int(time.time()) + LEASE_SECONDS)
        )
        session.commit()
        if claimed.rowcount != 1:
            raise HTTPException(409, "작업 처리 중입니다. 잠시 후 다시 확인해 주세요.")
        try:
            code_changes.apply(row, project)
        finally:
            row.lease_until = 0
            session.commit()
        return change_json(row)

    @routes.post("/changes/pr")
    def publish_change(
        project_id: str, body: ReviewInput, login=Depends(mutation), session=Depends(db)
    ):
        require_placeholder()
        project = access.real_project(project_id, login, session)
        row = get_change(project_id, session)
        if (
            not row.review_hash
            or row.content != code_changes.CONTENT
            or row.diff != code_changes.make_diff(row.content)
            or not secrets.compare_digest(body.review_hash, row.review_hash)
            or not secrets.compare_digest(row.review_hash, code_changes.review_hash(row, project))
        ):
            raise HTTPException(409, "변경 내용을 다시 검토한 뒤 PR 생성을 요청해 주세요.")
        if row.status == "pr_created":
            return change_json(row)
        claimed = session.execute(
            update(CodeChange)
            .where(
                CodeChange.id == row.id,
                CodeChange.status.in_(["applied", "publishing"]),
                CodeChange.lease_until <= int(time.time()),
            )
            .values(status="publishing", lease_until=int(time.time()) + LEASE_SECONDS, error="")
        )
        session.commit()
        if claimed.rowcount != 1:
            raise HTTPException(409, "PR 생성 요청을 처리 중입니다. 잠시 후 다시 확인해 주세요.")
        try:
            session.refresh(row)
            if not secrets.compare_digest(body.review_hash, code_changes.review_hash(row, project)):
                raise HTTPException(
                    409, "검토 이후 작업이 갱신되었습니다. 현재 변경 내용을 다시 확인해 주세요."
                )
            pr = code_changes.publish(github, access_token(login), project, row, session.commit)
            row.pr_url, row.pr_number, row.status = pr["html_url"], pr["number"], "pr_created"
        except (GitHubFailure, HTTPException) as error:
            row.status = "applied"
            row.error = (
                error.detail
                if isinstance(error, HTTPException)
                else "GitHub 게시에 실패했습니다. App 권한·브랜치 규칙·연결을 확인하고 다시 시도해 주세요. 생성된 브랜치와 PR은 재시도 시 확인합니다."
            )
            raise
        finally:
            row.lease_until = 0
            session.commit()
        return change_json(row)

    return routes

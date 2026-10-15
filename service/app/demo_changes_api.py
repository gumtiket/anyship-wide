"""데모 모드의 테스트 변경 경로: 등록, 로컬 적용, PR 미리보기(실제 GitHub 호출 없음)."""

import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import demo_changes
from .code_changes_api import ReviewInput
from .db import DemoChange


def demo_json(row):
    if not row:
        return None
    return {
        "id": row.id,
        "status": row.status,
        "mode": "demo",
        "file": demo_changes.FILE_NAME,
        "content": row.content,
        "diff": row.diff,
        "review_hash": row.review_hash,
        "branch": f"demo/placeholder-{row.id[:8]}",
        "commit_sha": row.commit_sha,
        "error": row.error,
        "github_pr_url": None,
        "pr_title": "[테스트] AI 분석 모듈 미연결 안내 추가",
    }


def router(settings, access, db, current, mutation):
    routes = APIRouter(prefix="/api/projects/{project_id}/demo-change")

    def demo_project(project_id, login, session):
        if not settings.demo:
            raise HTTPException(404, "테스트 모드에서만 사용할 수 있습니다.")
        return access.owned_project(project_id, login, session)

    def find(project_id, session):
        return session.scalar(select(DemoChange).where(DemoChange.project_id == project_id))

    def demo_change(project_id, login, session):
        demo_project(project_id, login, session)
        row = find(project_id, session)
        if not row:
            raise HTTPException(409, "먼저 테스트 수정 항목을 등록해 주세요.")
        return row

    @routes.get("")
    def get_demo_change(project_id: str, login=Depends(current), session=Depends(db)):
        demo_project(project_id, login, session)
        return demo_json(find(project_id, session))

    @routes.post("")
    def propose_demo_change(project_id: str, login=Depends(mutation), session=Depends(db)):
        demo_project(project_id, login, session)
        row = find(project_id, session)
        if row:
            return demo_json(row)
        row = DemoChange(id=str(uuid.uuid4()), project_id=project_id, content=demo_changes.CONTENT)
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            row = find(project_id, session)
        return demo_json(row)

    @routes.post("/apply")
    def apply_demo_change(project_id: str, login=Depends(mutation), session=Depends(db)):
        row = demo_change(project_id, login, session)
        if row.status in ("applied", "pr_preview"):
            return demo_json(row)
        claimed = session.execute(
            update(DemoChange)
            .where(DemoChange.id == row.id, DemoChange.status.in_(["proposed", "apply_failed"]))
            .values(status="applying", error="")
        )
        session.commit()
        if claimed.rowcount != 1:
            raise HTTPException(409, "현재 테스트 작업을 처리 중입니다.")
        try:
            row.diff, row.review_hash = demo_changes.apply(
                settings.demo_workspaces, row.id, row.content
            )
            row.status = "applied"
        except (demo_changes.DemoFailure, OSError):
            row.status = "apply_failed"
            row.error = "임시 파일 생성에 실패했습니다. Git 설치와 작업 폴더를 확인한 뒤 다시 실행해 주세요."
            session.commit()
            raise HTTPException(409, row.error) from None
        session.commit()
        return demo_json(row)

    @routes.post("/pr")
    def preview_demo_pr(
        project_id: str, body: ReviewInput, login=Depends(mutation), session=Depends(db)
    ):
        row = demo_change(project_id, login, session)
        if not row.review_hash or not secrets.compare_digest(body.review_hash, row.review_hash):
            raise HTTPException(409, "현재 변경 내용을 확인한 뒤 다시 요청해 주세요.")
        if row.status == "pr_preview":
            return demo_json(row)
        claimed = session.execute(
            update(DemoChange)
            .where(DemoChange.id == row.id, DemoChange.status == "applied")
            .values(status="publishing", error="")
        )
        session.commit()
        if claimed.rowcount != 1:
            raise HTTPException(409, "수정 실행과 검토를 먼저 완료해 주세요.")
        try:
            row.commit_sha = demo_changes.commit(
                settings.demo_workspaces, row.id, row.diff, row.content
            )
            row.status = "pr_preview"
        except (demo_changes.DemoFailure, OSError):
            row.status = "applied"
            row.error = "검토한 변경과 작업 파일이 일치하는지, Git 작업이 가능한지 확인해 주세요."
            session.commit()
            raise HTTPException(409, row.error) from None
        session.commit()
        return demo_json(row)

    return routes

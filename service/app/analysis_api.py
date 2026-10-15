import json
import secrets
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select, update

from . import analyses
from .analysis_contract import AnalysisRequest, ReviewRequest
from .analysis_source import AnalysisError
from .db import AnalysisRun
from .github_api import GitHubFailure


def router(
    settings, runner, github, db, current, mutation, owned_project, real_project, access_token
):
    routes = APIRouter(prefix="/api/projects/{project_id}/analyses", tags=["AI analyses"])

    def available():
        if settings.ai_mode != "bronze" or runner is None or runner.executor is None:
            raise HTTPException(503, "AI 분석 기능이 활성화되지 않았습니다.")

    def get_run(session, project_id, analysis_id):
        row = session.get(AnalysisRun, str(analysis_id))
        if not row or row.project_id != project_id:
            raise HTTPException(404, "분석 작업을 찾을 수 없습니다.")
        return row

    @routes.get("")
    def history(
        project_id: str,
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
        login=Depends(current),
        session=Depends(db),
    ):
        owned_project(project_id, login, session)
        return [
            analyses.run_json(row, detail=False)
            for row in session.scalars(
                select(AnalysisRun)
                .where(AnalysisRun.project_id == project_id)
                .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ]

    @routes.get("/{analysis_id}")
    def detail(
        project_id: str, analysis_id: uuid.UUID, login=Depends(current), session=Depends(db)
    ):
        owned_project(project_id, login, session)
        return analyses.run_json(get_run(session, project_id, analysis_id))

    @routes.post("", status_code=202)
    def start(
        project_id: str,
        body: AnalysisRequest,
        response: Response,
        login=Depends(mutation),
        session=Depends(db),
    ):
        available()
        project = real_project(project_id, login, session)
        try:
            row, created = runner.submit(session, project, body, access_token(login))
        except AnalysisError as error:
            raise HTTPException(409, {"code": error.code, "message": error.message}) from None
        response.status_code = 202 if created else 200
        return analyses.run_json(row)

    @routes.post("/{analysis_id}/pr")
    def publish(
        project_id: str,
        analysis_id: uuid.UUID,
        body: ReviewRequest,
        login=Depends(mutation),
        session=Depends(db),
    ):
        available()
        project = real_project(project_id, login, session)
        row = get_run(session, project_id, analysis_id)
        if (
            row.repository_id != project.repository_id
            or row.repository != project.full_name
            or row.base_branch != project.branch
        ):
            raise HTTPException(409, "프로젝트 연결이 변경되었습니다. 새로 분석해 주세요.")
        if (
            row.status != "completed"
            or not row.review_hash
            or not json.loads(row.files_json)
            or not secrets.compare_digest(body.review_hash, row.review_hash)
            or not secrets.compare_digest(row.review_hash, analyses.review_hash(row))
        ):
            raise HTTPException(409, "현재 변경안을 다시 검토해 주세요.")
        report = json.loads(row.report_json)
        if (
            report.get("status") == "failed"
            or report.get("transformation", {}).get("status") == "failed"
        ):
            raise HTTPException(409, "실패한 변경안은 게시할 수 없습니다.")
        if report.get("transformation", {}).get("needs_approval") and not body.approve_risky:
            raise HTTPException(409, "DB·저장 방식 등 위험 변경에 대한 명시적 승인이 필요합니다.")
        if row.publish_status == "pr_created":
            return analyses.run_json(row)
        claimed = session.execute(
            update(AnalysisRun)
            .where(
                AnalysisRun.id == row.id,
                AnalysisRun.status == "completed",
                AnalysisRun.publish_status.in_(("proposed", "publishing")),
                AnalysisRun.lease_until <= int(time.time()),
            )
            .values(
                publish_status="publishing",
                lease_until=int(time.time()) + 600,
                error="",
                error_code="",
            )
        )
        session.commit()
        if claimed.rowcount != 1:
            raise HTTPException(409, "PR 생성을 처리 중입니다. 잠시 후 확인해 주세요.")
        try:
            session.refresh(row)
            if not secrets.compare_digest(body.review_hash, analyses.review_hash(row)):
                raise HTTPException(409, "검토한 변경안이 갱신되었습니다.")
            pr = analyses.publish(github, access_token(login), row, session.commit)
            row.pr_url, row.pr_number, row.publish_status = (
                pr["html_url"],
                pr["number"],
                "pr_created",
            )
        except (GitHubFailure, HTTPException) as error:
            row.error_code = "publish_failed"
            row.error = (
                error.detail
                if isinstance(error, HTTPException)
                else "GitHub 게시에 실패했습니다. 같은 작업으로 재시도하면 기존 브랜치와 PR을 확인합니다."
            )
            raise
        finally:
            row.lease_until = 0
            session.commit()
        return analyses.run_json(row)

    return routes

"""전송 방식과 무관한 온프레미스 등록 경로. 콜백 자격 증명은 URL에 넣지 않는다."""

import hashlib
import json
import secrets
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .db import Deployment, OnpremEnvironment, OnpremJob, OnpremRegistrationToken
from .deploy_state import DeployStateError
from .deployments import DeploymentError
from .onprem_transport import error_message, guidance, record_report, require_kind

TOKEN_TTL = 15 * 60


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        min_length=3,
        max_length=254,
        pattern=r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$",
    )
    connection_kind: str = Field(default="ssh", max_length=24)


class TokenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=40, max_length=100, repr=False)


class ReportInput(TokenInput):
    public_ip: str = Field(min_length=7, max_length=45)


class EnvironmentJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    action: Literal["check", "remove_environment"]


def job_json(row):
    return {
        "id": row.id,
        "request_id": row.request_id,
        "action": row.action,
        "status": row.status,
        "logs": json.loads(row.logs_json),
        "result": json.loads(row.result_json),
        "created_at": row.created_at,
        "finished_at": row.finished_at,
    }


def router(settings, runner, db, current, mutation):
    routes = APIRouter(prefix="/api/onprem/environments", tags=["Onprem environments"])

    def runtime():
        worker = getattr(runner, "onprem", None)
        if worker is None or runner.executor is None:
            raise HTTPException(
                503,
                {"code": "onprem_unavailable", "message": "서버 연결 기능이 준비되지 않았습니다."},
            )
        return worker

    def fail(error):
        raise HTTPException(
            getattr(error, "status", 422), {"code": error.code, "message": error.message}
        )

    def owned(session, login, *, include_deleted=False):
        query = select(OnpremEnvironment).where(
            OnpremEnvironment.workspace_id == login.workspace_id,
            OnpremEnvironment.created_by == login.user_id,
        )
        return query if include_deleted else query.where(OnpremEnvironment.deleted_at.is_(None))

    def get_row(session, login, environment_id, *, include_deleted=False):
        row = session.scalar(
            owned(session, login, include_deleted=include_deleted).where(
                OnpremEnvironment.id == str(environment_id)
            )
        )
        if row is None:
            raise HTTPException(404, "환경을 찾을 수 없습니다.")
        return row

    def serialize(row):
        return {
            "id": row.id,
            "name": row.name,
            "connection_kind": row.connection_kind,
            "status": row.status,
            "public_ip": row.public_ip,
            "last_seen_at": row.last_seen_at,
            "active_job_id": row.active_job_id,
            "guidance": guidance(row.connection_kind),
            "error_code": row.error_code,
            "error_message": error_message(row.connection_kind, row.error_code)
            if row.error_code
            else "",
        }

    def issue_token(session, row):
        now, raw = int(time.time()), secrets.token_urlsafe(32)
        session.execute(
            update(OnpremRegistrationToken)
            .where(
                OnpremRegistrationToken.environment_id == row.id,
                OnpremRegistrationToken.used_at.is_(None),
            )
            .values(used_at=now)
        )
        session.add(
            OnpremRegistrationToken(
                token_hash=digest(raw), environment_id=row.id, expires_at=now + TOKEN_TTL
            )
        )
        command = runtime().transport.command(row, raw)
        return {"command": command, "expires_at": (now + TOKEN_TTL) * 1000}

    def token_row(session, environment_id, token, *, lock=False):
        if lock:
            # 재발급·삭제와 같은 순서(환경 → 토큰)로 잠근다.
            session.execute(
                update(OnpremEnvironment)
                .where(
                    OnpremEnvironment.id == str(environment_id),
                    OnpremEnvironment.deleted_at.is_(None),
                )
                .values(lease_until=OnpremEnvironment.lease_until)
            )
        now = int(time.time())
        valid = session.scalar(
            select(OnpremRegistrationToken).where(
                OnpremRegistrationToken.token_hash == digest(token),
                OnpremRegistrationToken.environment_id == str(environment_id),
                OnpremRegistrationToken.used_at.is_(None),
                OnpremRegistrationToken.expires_at > now,
            )
        )
        row = session.get(OnpremEnvironment, str(environment_id))
        if valid is None or row is None or row.deleted_at is not None:
            raise HTTPException(401, "등록 토큰이 만료되었거나 유효하지 않습니다.")
        return row, valid

    @routes.get("")
    def listing(login=Depends(current), session=Depends(db), limit: int = Query(50, ge=1, le=100)):
        runtime()
        return [
            serialize(row)
            for row in session.scalars(
                owned(session, login).order_by(OnpremEnvironment.created_at.desc()).limit(limit)
            )
        ]

    @routes.post("", status_code=201)
    def register(
        body: Registration, response: Response, login=Depends(mutation), session=Depends(db)
    ):
        runtime()
        response.headers["Cache-Control"] = "no-store"
        try:
            require_kind(body.connection_kind)
        except DeployStateError as error:
            fail(error)
        existing = session.scalar(
            select(OnpremEnvironment).where(
                OnpremEnvironment.workspace_id == login.workspace_id,
                OnpremEnvironment.created_by == login.user_id,
                OnpremEnvironment.request_id == str(body.request_id),
            )
        )
        if existing:
            if existing.deleted_at is not None or (
                existing.name,
                existing.email,
                existing.connection_kind,
            ) != (body.name, body.email, body.connection_kind):
                raise HTTPException(409, "같은 요청 ID로 다른 환경을 등록할 수 없습니다.")
            response.status_code = 200
            return {"environment": serialize(existing), "registration": None}
        identifier = str(uuid.uuid4())
        row = OnpremEnvironment(
            id=identifier,
            workspace_id=login.workspace_id,
            created_by=login.user_id,
            request_id=str(body.request_id),
            name=body.name,
            email=body.email,
            connection_kind=body.connection_kind,
            env_id="e" + uuid.UUID(identifier).hex[:20],
            created_at=int(time.time() * 1000),
        )
        session.add(row)
        try:
            session.flush()
            registration = issue_token(session, row)
            session.commit()
        except IntegrityError:
            session.rollback()
            return register(body, response, login, session)
        return {"environment": serialize(row), "registration": registration}

    @routes.post("/{environment_id}/registration")
    def reissue(
        environment_id: uuid.UUID, response: Response, login=Depends(mutation), session=Depends(db)
    ):
        runtime()
        response.headers["Cache-Control"] = "no-store"
        row = get_row(session, login, environment_id)
        locked = session.execute(
            update(OnpremEnvironment)
            .where(
                OnpremEnvironment.id == row.id,
                OnpremEnvironment.deleted_at.is_(None),
                OnpremEnvironment.active_job_id.is_(None),
            )
            .values(lease_until=OnpremEnvironment.lease_until)
        )
        if locked.rowcount != 1 or session.scalar(
            select(Deployment.project_id).where(Deployment.onprem_environment_id == row.id)
        ):
            session.rollback()
            raise HTTPException(409, "사용 중인 환경은 등록 명령을 다시 발급할 수 없습니다.")
        row.status, row.public_ip, row.last_seen_at, row.error_code = "ISSUED", None, None, ""
        registration = issue_token(session, row)
        session.commit()
        return registration

    @routes.post("/{environment_id}/setup", response_class=PlainTextResponse)
    def setup(environment_id: uuid.UUID, body: TokenInput, session=Depends(db)):
        worker = runtime()
        row, _ = token_row(session, environment_id, body.token)
        try:
            return PlainTextResponse(
                worker.transport.script(row, body.token), headers={"Cache-Control": "no-store"}
            )
        except Exception:
            raise HTTPException(
                503, "서버 준비 명령을 만들지 못했습니다. 서비스 설정을 확인하세요."
            ) from None

    @routes.post("/{environment_id}/ready", status_code=202)
    def ready(environment_id: uuid.UUID, body: ReportInput, session=Depends(db)):
        worker = runtime()
        row, valid = token_row(session, environment_id, body.token, lock=True)
        consumed = session.execute(
            update(OnpremRegistrationToken)
            .where(
                OnpremRegistrationToken.token_hash == valid.token_hash,
                OnpremRegistrationToken.used_at.is_(None),
                OnpremRegistrationToken.expires_at > int(time.time()),
            )
            .values(used_at=int(time.time()))
        )
        if consumed.rowcount != 1:
            session.rollback()
            raise HTTPException(401, "등록 토큰을 이미 사용했거나 만료되었습니다.")
        try:
            record_report(row, body.public_ip)
            row.status, row.error_code = "SIGNALED", ""
            job, _ = worker.environment_job(session, row, uuid.uuid4(), "check", commit=False)
            session.commit()
        except (DeployStateError, DeploymentError) as error:
            session.rollback()
            fail(error)
        worker.start_environment(job)
        return {"job_id": job.id, "status": "SIGNALED"}

    @routes.get("/{environment_id}/jobs")
    def jobs(environment_id: uuid.UUID, login=Depends(current), session=Depends(db)):
        runtime()
        row = get_row(session, login, environment_id, include_deleted=True)
        return [
            job_json(job)
            for job in session.scalars(
                select(OnpremJob)
                .where(OnpremJob.environment_id == row.id)
                .order_by(OnpremJob.created_at.desc(), OnpremJob.id.desc())
                .limit(50)
            )
        ]

    @routes.post("/{environment_id}/jobs", status_code=202)
    def submit(
        environment_id: uuid.UUID,
        body: EnvironmentJobInput,
        response: Response,
        login=Depends(mutation),
        session=Depends(db),
    ):
        worker = runtime()
        row = get_row(session, login, environment_id, include_deleted=True)
        try:
            job, created = worker.environment_job(session, row, body.request_id, body.action)
        except (DeployStateError, DeploymentError) as error:
            fail(error)
        if created:
            worker.start_environment(job)
        else:
            response.status_code = 200
        return job_json(job)

    return routes

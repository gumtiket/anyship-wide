"""User-owned AWS requests, durable role inputs and verified connections."""

import secrets
import time
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from .aws_adapter import ERRORS, AwsAdapter, AwsCheckError
from .aws_validation import role_account_id
from .db import AwsEnvironment
from .deployments import clear_real_targets
from .mock_deployments import clear_targets

REQUEST_TTL = 86400
VERIFICATION_LEASE = 120


def fail(status, code, message, retryable=False):
    raise HTTPException(status, {"code": code, "message": message, "retryable": retryable})


class EnvironmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    region: str = Field(min_length=1, max_length=32)


class VerifyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role_arn: str = Field(min_length=1, max_length=2048)

    @field_validator("role_arn")
    @classmethod
    def valid_role(cls, value):
        role_account_id(value)
        return value


def effective_status(row, now):
    if row.status != "CONNECTED" and row.expires_at <= now:
        return "EXPIRED"
    if row.status == "VERIFYING" and row.lease_until <= now:
        return "FAILED"
    return row.status


def summary(row):
    now = int(time.time())
    status = effective_status(row, now)
    error_code = row.error_code or None
    if status == "EXPIRED":
        error_code = "request_expired"
    elif row.status == "VERIFYING" and status == "FAILED":
        error_code = "verification_interrupted"
    url = None
    if status in ("PENDING", "FAILED"):
        parameters = {
            "templateURL": row.template_url,
            "stackName": row.stack_name,
            "param_ServiceRoleArn": row.service_role_arn,
            "param_ExternalId": row.external_id,
            "param_RoleName": row.role_name,
        }
        url = (
            f"https://{row.region}.console.aws.amazon.com/cloudformation/home?region={row.region}"
            "#/stacks/create/review?" + urlencode(parameters)
        )
    return {
        "id": row.id,
        "request_id": row.request_id,
        "name": row.name,
        "region": row.region,
        "status": status,
        "role_arn": row.role_arn,
        "aws_account_id": row.aws_account_id,
        "submitted_role_arn": row.submitted_role_arn,
        "stack_name": row.stack_name,
        "role_name": row.role_name,
        "cloudformation_url": url,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "verified_at": row.verified_at,
        "error_code": error_code,
        "retryable": status in ("PENDING", "FAILED"),
        "retry_after": max(0, row.lease_until - now) if status == "VERIFYING" else None,
    }


def router(settings, adapter: AwsAdapter | None, db, current, mutation):
    routes = APIRouter(prefix="/api/aws/environments", tags=["AWS environments"])

    def owned(login):
        return select(AwsEnvironment).where(
            AwsEnvironment.workspace_id == login.workspace_id,
            AwsEnvironment.created_by == login.user_id,
        )

    def require(session, login, identifier):
        row = session.scalar(owned(login).where(AwsEnvironment.id == identifier))
        if row is None:
            fail(404, "environment_not_found", "AWS 환경을 찾을 수 없습니다.")
        return row

    def configured():
        if not settings.aws_configured:
            fail(503, "aws_not_configured", "AWS 환경 등록을 위한 서비스 설정이 필요합니다.")

    @routes.get("")
    def list_environments(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        login=Depends(current),
        session=Depends(db),
    ):
        rows = session.scalars(
            owned(login)
            .order_by(AwsEnvironment.created_at.desc(), AwsEnvironment.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [summary(row) for row in rows]

    @routes.get("/{environment_id}")
    def get_environment(environment_id: uuid.UUID, login=Depends(current), session=Depends(db)):
        return summary(require(session, login, str(environment_id)))

    @routes.delete("/{environment_id}", status_code=204)
    def delete_environment(environment_id: uuid.UUID, login=Depends(mutation), session=Depends(db)):
        row = require(session, login, str(environment_id))
        session.refresh(row, with_for_update=True)
        clear_targets(session, environment_id=row.id)
        clear_real_targets(session, environment_id=row.id)
        removed = session.execute(
            delete(AwsEnvironment).where(
                AwsEnvironment.id == row.id,
                or_(
                    AwsEnvironment.status != "VERIFYING",
                    AwsEnvironment.lease_until <= int(time.time()),
                ),
            )
        )
        if removed.rowcount != 1:
            fail(
                409,
                "verification_in_progress",
                "연결 검증 중입니다. 완료 후 환경을 삭제해 주세요.",
                True,
            )
        session.commit()
        return Response(status_code=204)

    @routes.post("", status_code=201)
    def create_environment(
        body: EnvironmentInput, response: Response, login=Depends(mutation), session=Depends(db)
    ):
        request = owned(login).where(AwsEnvironment.request_id == str(body.request_id))

        def existing(row):
            if row.name != body.name or row.region != body.region:
                fail(
                    409,
                    "request_conflict",
                    "같은 request_id를 다른 환경 등록에 사용할 수 없습니다.",
                )
            response.status_code = 200
            return summary(row)

        row = session.scalar(request)
        if row:
            return existing(row)
        configured()
        if body.region not in settings.aws_regions:
            fail(422, "unsupported_region", "서비스에서 지원하는 AWS 리전을 선택해 주세요.")
        identifier = uuid.uuid4()
        now = int(time.time())
        row = AwsEnvironment(
            id=str(identifier),
            workspace_id=login.workspace_id,
            created_by=login.user_id,
            request_id=str(body.request_id),
            name=body.name,
            region=body.region,
            external_id=secrets.token_hex(32),
            template_url=settings.aws_template_url,
            service_role_arn=settings.aws_service_role_arn,
            stack_name="anyship-onboarding-" + identifier.hex,
            role_name=settings.aws_role_name.replace("{id}", identifier.hex),
            created_at=now,
            expires_at=now + REQUEST_TTL,
            status="PENDING",
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            row = session.scalar(request)
            if row:
                return existing(row)
            fail(
                409, "request_conflict", "환경 등록 요청이 충돌했습니다. 다시 시도해 주세요.", True
            )
        return summary(row)

    @routes.post("/{environment_id}/role")
    def save_role(
        environment_id: uuid.UUID, body: VerifyInput, login=Depends(mutation), session=Depends(db)
    ):
        identifier = str(environment_id)
        row = require(session, login, identifier)
        if row.status == "CONNECTED":
            if row.role_arn != body.role_arn:
                fail(409, "already_connected", "이미 연결된 환경의 역할은 변경할 수 없습니다.")
            return summary(row)
        now = int(time.time())
        if row.expires_at <= now:
            fail(
                410, "request_expired", "환경 등록 요청이 만료되었습니다. 새 요청을 만들어 주세요."
            )
        # Saving an input needs no AWS configuration or credentials. An active
        # verification must finish before another input can replace its candidate.
        saved = session.execute(
            update(AwsEnvironment)
            .where(
                AwsEnvironment.id == identifier,
                AwsEnvironment.expires_at > now,
                or_(
                    AwsEnvironment.status.in_(("PENDING", "FAILED")),
                    and_(AwsEnvironment.status == "VERIFYING", AwsEnvironment.lease_until <= now),
                ),
            )
            .values(
                submitted_role_arn=body.role_arn,
                status="PENDING",
                error_code="",
                verification_token="",
                lease_until=0,
            )
        )
        session.commit()
        if saved.rowcount != 1:
            fail(
                409,
                "verification_in_progress",
                "검증 중이거나 상태가 변경되었습니다. 환경 상태를 다시 조회해 주세요.",
                True,
            )
        session.expire_all()
        return summary(require(session, login, identifier))

    @routes.post("/{environment_id}/verify")
    def verify(
        environment_id: uuid.UUID, body: VerifyInput, login=Depends(mutation), session=Depends(db)
    ):
        identifier = str(environment_id)
        row = require(session, login, identifier)
        if row.status == "CONNECTED":
            if row.role_arn != body.role_arn:
                fail(409, "already_connected", "이미 연결된 환경의 역할은 변경할 수 없습니다.")
            return summary(row)
        configured()
        now = int(time.time())
        if row.expires_at <= now:
            fail(
                410, "request_expired", "환경 등록 요청이 만료되었습니다. 새 요청을 만들어 주세요."
            )
        if (
            row.region not in settings.aws_regions
            or row.service_role_arn != settings.aws_service_role_arn
        ):
            fail(
                409,
                "configuration_changed",
                "등록 후 AWS 연결 설정이 변경되었습니다. 새 요청을 만들어 주세요.",
            )
        if adapter is None:
            # Preserve the input without claiming verification or marking a connection successful.
            save_role(environment_id, body, login, session)
            status, message = ERRORS["aws_adapter_unavailable"]
            fail(status, "aws_adapter_unavailable", message, True)
        token = str(uuid.uuid4())
        claim = session.execute(
            update(AwsEnvironment)
            .where(
                AwsEnvironment.id == identifier,
                AwsEnvironment.expires_at > now,
                or_(
                    AwsEnvironment.status.in_(("PENDING", "FAILED")),
                    and_(AwsEnvironment.status == "VERIFYING", AwsEnvironment.lease_until <= now),
                ),
            )
            .values(
                submitted_role_arn=body.role_arn,
                status="VERIFYING",
                verification_token=token,
                lease_until=now + VERIFICATION_LEASE,
                error_code="",
            )
        )
        session.commit()
        if claim.rowcount != 1:
            fail(
                409,
                "verification_in_progress",
                "이미 검증 중이거나 상태가 변경되었습니다. 환경 상태를 다시 조회해 주세요.",
                True,
            )
        external_id, region = row.external_id, row.region
        # No database lock/transaction is held while calling AWS.
        condition = (
            AwsEnvironment.id == identifier,
            AwsEnvironment.verification_token == token,
            AwsEnvironment.status == "VERIFYING",
        )

        def finish_failure(code):
            finished = int(time.time())
            expired = row.expires_at <= finished
            changed = session.execute(
                update(AwsEnvironment)
                .where(*condition)
                .values(
                    status="EXPIRED" if expired else "FAILED",
                    error_code=code,
                    verification_token="",
                    lease_until=0,
                )
            )
            session.commit()
            if changed.rowcount != 1:
                fail(
                    409,
                    "verification_superseded",
                    "검증 요청이 다른 요청으로 대체되었습니다. 상태를 다시 조회해 주세요.",
                    True,
                )
            if expired:
                fail(
                    410,
                    "request_expired",
                    "검증 중 환경 등록 요청이 만료되었습니다. 새 요청을 만들어 주세요.",
                )

        try:
            identity = adapter.check(role_arn=body.role_arn, external_id=external_id, region=region)
            if identity.account_id != role_account_id(body.role_arn):
                raise AwsCheckError("account_mismatch")
        except Exception as error:
            # Neither AWS error strings nor an adapter's raw exception can enter the DB/response.
            code = (
                error.code
                if isinstance(error, AwsCheckError) and error.code in ERRORS
                else "aws_unavailable"
            )
            finish_failure(code)
            status_code, message = ERRORS[code]
            fail(status_code, code, message, True)
        finished = int(time.time())
        if row.expires_at <= finished:
            finish_failure("request_expired")
        try:
            saved = session.execute(
                update(AwsEnvironment)
                .where(*condition, AwsEnvironment.expires_at > finished)
                .values(
                    status="CONNECTED",
                    role_arn=body.role_arn,
                    aws_account_id=identity.account_id,
                    verified_at=finished,
                    error_code="",
                    verification_token="",
                    lease_until=0,
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            finish_failure("role_already_registered")
            fail(409, "role_already_registered", "이 워크스페이스에 이미 등록된 AWS 역할입니다.")
        if saved.rowcount != 1:
            fail(
                409,
                "verification_superseded",
                "검증 요청이 다른 요청으로 대체되었습니다. 상태를 다시 조회해 주세요.",
                True,
            )
        session.expire_all()
        return summary(require(session, login, identifier))

    return routes

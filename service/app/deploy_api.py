"""실제 배포 API. 모의 배포(`/mock-deployment`)와 경로와 코드가 분리되어 있다.

`POST /jobs`는 배포(`action` 기본값)와 배포 삭제(`"destroy"`)를 같은 선점, 멱등, 로그 저장으로 처리한다. 삭제는 앱의 컨테이너와 서버의 앱 폴더만
지운다(앱 DB, 공용 기반, DNS는 남는다). 본문에는 사용자가 입력한 비밀이 들어 있을 수 있다(삭제에는 비밀을 받지 않는다). 비밀은 실행기로 메모리에서만 넘기고 응답, 로그, DB 어디에도 남기지 않는다.
검증 오류 응답에 입력값이 섞이지 않도록 이 경로의 422는 `app.py`의 처리기가 고정 문구로 바꾼다.
"""

import re
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from .db import AwsEnvironment, DeployJob, Deployment, OnpremEnvironment, Project
from .deploy_state import DeployStateError
from .deployments import REAL_SETS, DeploymentError, job_json, select_target
from .errors import api_error
from .source import SourceError

_SECRET_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
MAX_SECRETS, MAX_SECRET_LENGTH = 50, 4096


class TargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment_id: uuid.UUID
    set_name: str = Field(min_length=1, max_length=24)


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    action: Literal["deploy", "destroy", "status", "rollback", "migrate", "destroy_previous"] = (
        "deploy"
    )
    target_environment_id: uuid.UUID | None = None  # migrate만: 옮겨 갈 환경
    target_set_name: str = Field(default="", max_length=24)
    image_tag: str = Field(default="", max_length=40)
    secrets: dict[str, str] = Field(default_factory=dict, max_length=MAX_SECRETS)

    @field_validator("secrets")
    @classmethod
    def secret_names_and_sizes(cls, value):
        # 오류 문구에 값이 들어가지 않게 이름 규칙과 길이만 본다.
        if any(
            not _SECRET_NAME.match(name) or len(item) > MAX_SECRET_LENGTH
            for name, item in value.items()
        ):
            raise ValueError("invalid secrets")
        return value

    @model_validator(mode="after")
    def destroy_takes_no_secrets(self):
        if self.action not in ("deploy", "migrate") and self.secrets:
            raise ValueError("a destroy takes no secrets")  # 값은 오류에 싣지 않는다
        if self.action == "migrate":
            if self.target_environment_id is None or self.target_set_name not in REAL_SETS:
                raise ValueError("a migration needs a target environment and set")
        elif self.target_environment_id is not None or self.target_set_name:
            raise ValueError("only a migration takes a target")
        if self.action == "rollback":
            if not re.fullmatch(r"[0-9a-f]{7,40}", self.image_tag):
                raise ValueError("invalid rollback image tag")
        elif self.image_tag:
            raise ValueError("only rollback accepts an image tag")
        return self


def router(settings, runner, db, current, mutation, access_token=None):
    routes = APIRouter(prefix="/api/projects/{project_id}/deployment", tags=["Deployments"])

    def project(session, login, project_id):
        row = session.scalar(
            select(Project).where(
                Project.id == project_id,
                Project.workspace_id == login.workspace_id,
                Project.created_by == login.user_id,
            )
        )
        if row is None:
            api_error(404, "project_not_found", "프로젝트를 찾을 수 없습니다.")
        if runner is None or runner.executor is None:
            api_error(503, "deploy_unavailable", "실제 배포 기능이 활성화되지 않았습니다.")
        return row

    def owned_environments(session, login):
        return select(AwsEnvironment).where(
            AwsEnvironment.workspace_id == login.workspace_id,
            AwsEnvironment.created_by == login.user_id,
        )

    def target_json(session, target):
        if target is None:
            return None
        environment = (
            session.get(OnpremEnvironment, target.onprem_environment_id)
            if target.onprem_environment_id
            else session.get(AwsEnvironment, target.aws_environment_id)
        )
        return {
            "environment_id": environment.id,
            "environment_name": environment.name,
            "set_name": target.set_name,
            "app_name": target.app_name,
            "image_tag": target.image_tag,
            "url": target.url,
            "deployed": bool(
                target.image_tag or (target.onprem_environment_id and target.app_name)
            ),
            "active_job_id": target.active_job_id,
            "busy": bool(target.active_job_id) and target.lease_until > int(time.time()),
            "previous": previous_json(session, target),
        }

    def previous_json(session, target):
        """환경 이전 뒤 원래 환경에 멈춘 채 남은 앱(없으면 None). 사용자가 확인하고 지울 때까지 보여 준다."""
        if not target.previous_app_name:
            return None
        row = session.get(
            OnpremEnvironment if target.previous_kind == "onprem" else AwsEnvironment,
            target.previous_environment_id,
        )
        return {
            "kind": target.previous_kind,
            "environment_name": row.name if row else "(삭제된 환경)",
            "set_name": "onprem" if target.previous_kind == "onprem" else "aws-always-on",
            "app_name": target.previous_app_name,
            "url": target.previous_url,
        }

    @routes.get("")
    def get(project_id: str, login=Depends(current), session=Depends(db)):
        project(session, login, project_id)
        rows = session.scalars(
            owned_environments(session, login).order_by(AwsEnvironment.created_at.desc())
        )
        environments = [
            {
                "id": row.id,
                "name": row.name,
                "region": row.region,
                "set_name": "aws-always-on",
                "available": row.status == "CONNECTED" and bool(row.role_arn),
            }
            for row in rows
        ]
        onprem = getattr(runner, "onprem", None)
        if onprem:
            environments += [
                {
                    "id": row.id,
                    "name": row.name,
                    "region": "온프레미스",
                    "set_name": "onprem",
                    "available": row.status == "VERIFIED",
                }
                for row in session.scalars(
                    select(OnpremEnvironment).where(
                        OnpremEnvironment.workspace_id == login.workspace_id,
                        OnpremEnvironment.created_by == login.user_id,
                        OnpremEnvironment.deleted_at.is_(None),
                    )
                )
            ]
        return {
            "target": target_json(session, session.get(Deployment, project_id)),
            "sets": list(REAL_SETS) if onprem else ["aws-always-on"],
            "environments": environments,
        }

    def environment_for(session, login, environment_id, set_name):
        """요청한 사용자가 소유한 환경을 찾는다(없으면 404)."""
        if set_name == "onprem" and getattr(runner, "onprem", None):
            environment = session.scalar(
                select(OnpremEnvironment).where(
                    OnpremEnvironment.id == str(environment_id),
                    OnpremEnvironment.workspace_id == login.workspace_id,
                    OnpremEnvironment.created_by == login.user_id,
                    OnpremEnvironment.deleted_at.is_(None),
                )
            )
        else:
            environment = session.scalar(
                owned_environments(session, login).where(AwsEnvironment.id == str(environment_id))
            )
        if environment is None:
            api_error(404, "environment_not_found", "환경을 찾을 수 없습니다.")
        return environment

    @routes.put("")
    def save(project_id: str, body: TargetInput, login=Depends(mutation), session=Depends(db)):
        project(session, login, project_id)
        environment = environment_for(session, login, body.environment_id, body.set_name)
        try:
            return target_json(
                session, select_target(session, project_id, environment, body.set_name)
            )
        except (DeploymentError, DeployStateError) as error:
            api_error(getattr(error, "status", 409), error.code, error.message)

    @routes.get("/jobs")
    def jobs(
        project_id: str,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        login=Depends(current),
        session=Depends(db),
    ):
        project(session, login, project_id)
        return [
            job_json(row)
            for row in session.scalars(
                select(DeployJob)
                .where(DeployJob.project_id == project_id)
                .order_by(DeployJob.created_at.desc(), DeployJob.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ]

    @routes.get("/jobs/{job_id}")
    def job(project_id: str, job_id: uuid.UUID, login=Depends(current), session=Depends(db)):
        project(session, login, project_id)
        row = session.get(DeployJob, str(job_id))
        if row is None or row.project_id != project_id:
            api_error(404, "job_not_found", "작업을 찾을 수 없습니다.")
        return job_json(row)

    @routes.post("/jobs", status_code=202)
    def submit(
        project_id: str,
        body: JobInput,
        response: Response,
        login=Depends(mutation),
        session=Depends(db),
    ):
        row = project(session, login, project_id)
        try:
            if body.action in ("status", "rollback"):
                target = session.get(Deployment, row.id)
                if (
                    not target
                    or not target.onprem_environment_id
                    or not getattr(runner, "onprem", None)
                ):
                    api_error(
                        422, "action_not_supported", "이 환경에서는 지원하지 않는 작업입니다."
                    )
                created_job, created = runner.onprem.submit_project(
                    session, row, body.request_id, body.action, image_tag=body.image_tag
                )
            elif body.action in ("migrate", "destroy_previous"):
                migration = getattr(runner, "migration", None)
                if migration is None:
                    api_error(
                        422, "action_not_supported", "이 서버에서는 환경 이전을 지원하지 않습니다."
                    )
                if body.action == "destroy_previous":
                    created_job, created = migration.submit_destroy_previous(
                        session, row, body.request_id
                    )
                else:
                    destination = environment_for(
                        session, login, body.target_environment_id, body.target_set_name
                    )
                    token = access_token(login) if access_token else None
                    created_job, created = migration.submit_migrate(
                        session, row, body.request_id, destination, body.secrets, token
                    )
            elif body.action == "destroy":
                created_job, created = runner.submit_destroy(session, row, body.request_id)
            else:
                # 사용자의 GitHub 토큰은 요청 안에서 소스를 받을 때만 쓰이고, 응답, 로그, DB에는 남지 않는다.
                token = access_token(login) if access_token else None
                created_job, created = runner.submit(
                    session, row, body.request_id, body.secrets, token=token
                )
        except DeploymentError as error:
            api_error(error.status, error.code, error.message)
        except (DeployStateError, SourceError) as error:
            api_error(409, error.code, error.message)
        response.status_code = 202 if created else 200
        return job_json(created_job)

    return routes

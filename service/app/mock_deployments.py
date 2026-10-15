"""인프라의 공개 어댑터 계약을 쓰는 명시적 개발용 모의 배포.

실제 어댑터로 대체 실행하지 않고, GitHub 호출·AWS 확인 결과 저장·비밀 입력도 없다.
DB별 프로세스 잠금으로 메모리에만 있는 어댑터 상태를 한 작업자 안에 둔다.
"""

import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from .db import AwsEnvironment, MockDeployment, MockJob, Project
from .errors import api_error

ACTIVE = ("queued", "running")
SETS = {"aws": ("aws-serverless", "aws-always-on"), "onprem": ("onprem",)}
SAMPLES = {"sample-aws": ("샘플 AWS", "aws"), "sample-onprem": ("샘플 온프레미스", "onprem")}


def clear_targets(session, *, project_id=None, environment_id=None):
    """작업 선점과 원자적으로 경쟁한다. 삭제 트랜잭션은 호출한 쪽이 관리한다."""
    condition = (
        MockDeployment.project_id == project_id
        if project_id is not None
        else MockDeployment.aws_environment_id == environment_id
    )
    session.execute(delete(MockDeployment).where(condition, MockDeployment.active_job_id.is_(None)))
    if session.scalar(select(MockDeployment.project_id).where(condition)):
        api_error(409, "mock_job_running", "모의 배포 작업 중입니다. 완료 후 삭제해 주세요.")
    if project_id is not None:
        session.execute(delete(MockJob).where(MockJob.project_id == project_id))


class TargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=36)
    set_name: Literal["aws-serverless", "aws-always-on", "onprem"]


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    action: Literal["check", "deploy", "rollback", "destroy"]
    scenario: Literal["success", "check_fails", "deploy_fails", "unhealthy"] = "success"
    image_tag: str = Field(default="", pattern=r"^(?:[0-9a-f]{7,40})?$")


def job_json(row):
    return {
        "id": row.id,
        "request_id": row.request_id,
        "mock": True,
        "action": row.action,
        "scenario": row.scenario,
        "target_label": row.target_label,
        "set_name": row.set_name,
        "image_tag": row.image_tag,
        "status": row.status,
        "logs": json.loads(row.logs_json),
        "result": json.loads(row.result_json),
        "created_at": row.created_at,
        "finished_at": row.finished_at,
    }


class MockRunner:
    def __init__(self, settings, sessions):
        from anyship_adapters import MockAdapter

        self.settings, self.sessions = settings, sessions
        self.adapter_factory = MockAdapter
        self.runtime_id = str(uuid.uuid4())
        self.guard = threading.RLock()
        self.adapters = {}
        self.executor = None
        self.lock_file = None

    def start(self):
        directory = Path(__file__).resolve().parents[1] / "workspaces" / "mock"
        directory.mkdir(parents=True, exist_ok=True)
        identifier = hashlib.sha256(self.settings.database_url.encode()).hexdigest()
        handle = (directory / (identifier + ".lock")).open("a+b")
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError(
                "Mock mode supports one Service process per database; stop the other worker."
            ) from None
        self.lock_file = handle
        try:
            self.runtime_id = str(uuid.uuid4())
            self.adapters.clear()
            with self.sessions() as session:
                session.execute(
                    update(MockJob)
                    .where(MockJob.status.in_(ACTIVE))
                    .values(
                        status="interrupted",
                        finished_at=int(time.time() * 1000),
                        result_json=json.dumps(
                            {
                                "ok": False,
                                "error": {
                                    "code": "worker_restarted",
                                    "message": "서버가 재시작되어 작업이 중단됐습니다. 새 요청으로 다시 시험해 주세요.",
                                    "retryable": True,
                                },
                            }
                        ),
                    )
                )
                session.execute(
                    update(MockDeployment).values(active_job_id=None, checked_runtime_id="")
                )
                session.commit()
            self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="anyship-mock")
        except Exception:
            handle.close()
            self.lock_file = None
            raise

    def close(self):
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None
            with self.sessions() as session:
                pending_ids = list(
                    session.scalars(
                        select(MockJob.id).where(
                            MockJob.runtime_id == self.runtime_id, MockJob.status.in_(ACTIVE)
                        )
                    )
                )
                if pending_ids:
                    session.execute(
                        update(MockJob)
                        .where(MockJob.id.in_(pending_ids))
                        .values(
                            status="interrupted",
                            finished_at=int(time.time() * 1000),
                            result_json=json.dumps(
                                {
                                    "ok": False,
                                    "error": {
                                        "code": "worker_stopping",
                                        "message": "서버 종료로 작업이 중단됐습니다. 다시 시험해 주세요.",
                                        "retryable": True,
                                    },
                                }
                            ),
                        )
                    )
                    session.execute(
                        update(MockDeployment)
                        .where(MockDeployment.active_job_id.in_(pending_ids))
                        .values(active_job_id=None)
                    )
                    session.commit()
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def adapter(self, target):
        # 선택이 바뀌지 않는 동안 어댑터 하나를 유지해 실패 시나리오 사이의 상태를 이어 간다.
        if target.adapter_env_id not in self.adapters:
            self.adapters[target.adapter_env_id] = self.adapter_factory(
                delay=self.settings.mock_step_delay
            )
        return self.adapters[target.adapter_env_id]

    def environment(self, target, session):
        from anyship_adapters import AwsEnvironment as AdapterAWS
        from anyship_adapters import OnpremEnvironment

        if target.kind == "onprem":
            return OnpremEnvironment(env_id=target.adapter_env_id, host="203.0.113.10")
        if target.aws_environment_id:
            row = session.get(AwsEnvironment, target.aws_environment_id)
            if row is None or not (row.submitted_role_arn or row.role_arn):
                api_error(409, "role_required", "선택한 AWS 환경에 Role ARN을 먼저 저장해 주세요.")
            return AdapterAWS(
                env_id=target.adapter_env_id,
                role_arn=row.submitted_role_arn or row.role_arn,
                external_id=row.external_id,
                region=row.region,
            )
        return AdapterAWS(
            env_id=target.adapter_env_id,
            role_arn="arn:aws:iam::123456789012:role/mock-deploy",
            external_id="mock-external-id-for-development-only",
            region="ap-northeast-2",
        )

    def versions(self, session, target):
        rows = session.scalars(
            select(MockJob.image_tag)
            .where(
                MockJob.project_id == target.project_id,
                MockJob.adapter_env_id == target.adapter_env_id,
                MockJob.runtime_id == self.runtime_id,
                MockJob.action == "deploy",
                MockJob.status == "succeeded",
            )
            .order_by(MockJob.created_at.desc())
        )
        return list(dict.fromkeys(rows))

    def target_json(self, target, session):
        if target is None:
            return None
        env = self.environment(target, session)
        status = self.adapter(target).status(env, "mock-app")
        return {
            "source": target.source,
            "label": target.label,
            "kind": target.kind,
            "set_name": target.set_name,
            "mock": True,
            "checked": target.checked_runtime_id == self.runtime_id,
            "active_job_id": target.active_job_id,
            "state": status.state,
            "image_tag": status.image_tag,
            "example_url": status.url,
            "versions": self.versions(session, target),
        }

    def execute(self, job_id, env):
        from anyship_adapters import redact_event, redact_model

        with self.guard, self.sessions() as session:
            row = session.get(MockJob, job_id)
            target = session.get(MockDeployment, row.project_id)
            adapter = self.adapter(target)
            adapter.scenario = row.scenario
            row.status = "running"
            action, image_tag = row.action, row.image_tag
            project_id, set_name = row.project_id, row.set_name
            if action in ("check", "deploy"):
                target.checked_runtime_id = ""
            session.commit()

        def log(event):
            safe = redact_event(event).model_dump(mode="json")
            # 임의의 진단 데이터를 서비스 DB에 복사하지 않는다.
            safe.pop("data", None)
            with self.sessions() as session:
                row = session.get(MockJob, job_id)
                events = json.loads(row.logs_json)
                if len(events) < 100:
                    events.append(safe)
                row.logs_json = json.dumps(events, ensure_ascii=False)
                session.commit()

        checked = False
        try:
            if action in ("check", "deploy"):
                result = adapter.check(env, log)
                checked = result.ok
                if checked and action == "deploy":
                    result = adapter.deploy(
                        env,
                        {"app": "mock-app", "backing_services": []},
                        image_tag,
                        {},
                        log,
                        set_name=set_name,
                    )
            elif action == "rollback":
                result = adapter.rollback(env, "mock-app", image_tag, log)
            else:
                result = adapter.destroy(env, "mock-app", log)
            result_data = redact_model(result).model_dump(mode="json")
            state = "succeeded" if result.ok else "failed"
        except Exception:
            # 예외에는 자격 증명이 담긴 진단 정보가 있을 수 있으므로 저장하지 않는다.
            result_data = {
                "ok": False,
                "error": {
                    "code": "adapter_error",
                    "message": "모의 작업 처리 중 오류가 발생했습니다. 다시 시험해 주세요.",
                    "retryable": True,
                },
            }
            state = "failed"
        with self.guard, self.sessions() as session:
            row = session.get(MockJob, job_id)
            row.status, row.result_json, row.finished_at = (
                state,
                json.dumps(result_data, ensure_ascii=False),
                int(time.time() * 1000),
            )
            session.execute(
                update(MockDeployment)
                .where(
                    MockDeployment.project_id == project_id, MockDeployment.active_job_id == job_id
                )
                .values(
                    active_job_id=None,
                    **(
                        {"checked_runtime_id": self.runtime_id if checked else ""}
                        if action in ("check", "deploy")
                        else {}
                    ),
                )
            )
            session.commit()


def router(settings, runner, db, current, mutation):
    routes = APIRouter(
        prefix="/api/projects/{project_id}/mock-deployment", tags=["Mock deployments"]
    )

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
            api_error(503, "mock_unavailable", "모의 배포 기능이 활성화되지 않았습니다.")
        return row

    @routes.get("")
    def get(project_id: str, login=Depends(current), session=Depends(db)):
        project(session, login, project_id)
        options = [
            {"source": key, "label": label, "kind": kind, "sets": SETS[kind], "available": True}
            for key, (label, kind) in SAMPLES.items()
        ]
        rows = session.scalars(
            select(AwsEnvironment)
            .where(
                AwsEnvironment.workspace_id == login.workspace_id,
                AwsEnvironment.created_by == login.user_id,
            )
            .order_by(AwsEnvironment.created_at.desc())
        )
        options.extend(
            {
                "source": row.id,
                "label": row.name + " (등록 AWS 복사본)",
                "kind": "aws",
                "sets": SETS["aws"],
                "available": bool(row.submitted_role_arn or row.role_arn),
            }
            for row in rows
        )
        with runner.guard:
            target = runner.target_json(session.get(MockDeployment, project_id), session)
        return {"mock": True, "target": target, "environments": options}

    @routes.put("")
    def save(project_id: str, body: TargetInput, login=Depends(mutation), session=Depends(db)):
        project(session, login, project_id)
        with runner.guard:
            target = session.get(MockDeployment, project_id)
            if target and target.source == body.source and target.set_name == body.set_name:
                return runner.target_json(target, session)
            if target and (
                target.active_job_id
                or runner.target_json(target, session)["state"] != "not_deployed"
            ):
                api_error(
                    409,
                    "target_in_use",
                    "진행 중 작업을 마치고 모의 배포를 제거한 뒤 환경을 변경해 주세요.",
                )
            aws_id = None
            if body.source in SAMPLES:
                label, kind = SAMPLES[body.source]
            else:
                row = session.scalar(
                    select(AwsEnvironment)
                    .where(
                        AwsEnvironment.id == body.source,
                        AwsEnvironment.workspace_id == login.workspace_id,
                        AwsEnvironment.created_by == login.user_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    api_error(404, "environment_not_found", "환경을 찾을 수 없습니다.")
                if not (row.submitted_role_arn or row.role_arn):
                    api_error(409, "role_required", "Role ARN을 먼저 저장해 주세요.")
                aws_id, label, kind = row.id, row.name, "aws"
            if body.set_name not in SETS[kind]:
                api_error(422, "set_not_supported", "환경에 맞는 배포 방식을 선택해 주세요.")
            if target:
                removed = session.execute(
                    delete(MockDeployment).where(
                        MockDeployment.project_id == project_id,
                        MockDeployment.active_job_id.is_(None),
                    )
                )
                if removed.rowcount != 1:
                    api_error(409, "mock_job_running", "모의 작업이 진행 중입니다.")
                session.flush()
            new = MockDeployment(
                project_id=project_id,
                source=body.source,
                aws_environment_id=aws_id,
                label=label,
                kind=kind,
                adapter_env_id="e" + secrets.token_hex(10),
                set_name=body.set_name,
            )
            session.add(new)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                api_error(
                    409,
                    "selection_conflict",
                    "프로젝트 또는 환경이 변경되었습니다. 새로고침해 주세요.",
                )
            return runner.target_json(new, session)

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
                select(MockJob)
                .where(MockJob.project_id == project_id)
                .order_by(MockJob.created_at.desc(), MockJob.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ]

    @routes.get("/jobs/{job_id}")
    def job(project_id: str, job_id: uuid.UUID, login=Depends(current), session=Depends(db)):
        project(session, login, project_id)
        row = session.get(MockJob, str(job_id))
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
        project(session, login, project_id)
        with runner.guard:
            previous = session.scalar(
                select(MockJob).where(
                    MockJob.project_id == project_id, MockJob.request_id == str(body.request_id)
                )
            )
            if previous:
                if (previous.action, previous.scenario, previous.image_tag) != (
                    body.action,
                    body.scenario,
                    body.image_tag,
                ):
                    api_error(
                        409, "request_conflict", "같은 요청 식별자에 다른 작업을 보낼 수 없습니다."
                    )
                response.status_code = 200
                return job_json(previous)
            target = session.get(MockDeployment, project_id)
            if target is None:
                api_error(409, "target_required", "테스트할 환경을 먼저 선택해 주세요.")
            if body.action in ("deploy", "rollback") and not body.image_tag:
                api_error(422, "image_tag_required", "테스트 버전을 입력해 주세요.")
            if body.action in ("check", "destroy") and body.image_tag:
                api_error(422, "unexpected_image_tag", "이 작업에는 테스트 버전을 보내지 마세요.")
            env = runner.environment(target, session)
            if body.action == "deploy" and target.checked_runtime_id != runner.runtime_id:
                api_error(409, "check_required", "먼저 모의 연결 확인을 완료해 주세요.")
            if body.action == "rollback" and body.image_tag not in runner.versions(session, target):
                api_error(
                    409,
                    "version_not_found",
                    "현재 서버 실행 중 배포에 성공한 버전을 선택해 주세요.",
                )
            identifier = str(uuid.uuid4())
            claimed = session.execute(
                update(MockDeployment)
                .where(
                    MockDeployment.project_id == project_id, MockDeployment.active_job_id.is_(None)
                )
                .values(active_job_id=identifier)
            )
            if claimed.rowcount != 1:
                api_error(409, "mock_job_running", "이미 모의 작업을 처리하고 있습니다.")
            row = MockJob(
                id=identifier,
                project_id=project_id,
                request_id=str(body.request_id),
                runtime_id=runner.runtime_id,
                adapter_env_id=target.adapter_env_id,
                target_label=target.label,
                set_name=target.set_name,
                action=body.action,
                scenario=body.scenario,
                image_tag=body.image_tag,
                created_at=int(time.time() * 1000),
            )
            session.add(row)
            session.commit()
            try:
                runner.executor.submit(runner.execute, identifier, env)
            except RuntimeError:
                row.status = "interrupted"
                row.finished_at = int(time.time() * 1000)
                row.result_json = json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "worker_stopping",
                            "message": "서버 종료 중입니다. 다시 시험해 주세요.",
                        },
                    }
                )
                session.execute(
                    update(MockDeployment)
                    .where(MockDeployment.active_job_id == identifier)
                    .values(active_job_id=None)
                )
                session.commit()
            return job_json(row)

    return routes

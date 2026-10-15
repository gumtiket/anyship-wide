"""Fixed on-premise operations, using the existing worker and environment-wide leases."""

import json
import time
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from anyship_adapters import redact_model

from .db import DeployJob, Deployment, OnpremEnvironment, OnpremJob, OnpremRegistrationToken
from .deploy_state import DeployStateError
from .deployments import ACTIVE, DeploymentError, add_events, claim, find_request, finish
from .onprem_state import acquire, extend, in_use, release
from .onprem_transport import environment, present_result, verified_address


class OnpremRunner:
    def __init__(self, parent, deployer, transport):
        self.parent, self.deployer, self.transport = parent, deployer, transport
        self.sessions = parent.sessions

    def environment_job(self, session, row, request_id, action, *, commit=True):
        if action not in ("check", "remove_environment"):
            raise DeploymentError(422, "action_invalid", "지원하지 않는 환경 작업입니다.")
        existing = session.scalar(
            select(OnpremJob).where(
                OnpremJob.environment_id == row.id, OnpremJob.request_id == str(request_id)
            )
        )
        if existing:
            if existing.action != action:
                raise DeploymentError(409, "request_conflict", "다른 작업에 사용한 요청 ID입니다.")
            return existing, False
        identifier = str(uuid.uuid4())
        try:
            acquire(session, row.id, identifier)
        except DeploymentError:
            # A concurrent identical request may have committed while acquire waited.
            if session.scalar(
                select(OnpremJob.id).where(
                    OnpremJob.environment_id == row.id, OnpremJob.request_id == str(request_id)
                )
            ):
                return self.environment_job(session, row, request_id, action, commit=commit)
            raise
        session.refresh(row)
        if action == "check":
            environment(row, require_verified=False)
        if action == "remove_environment" and in_use(session, row.id):
            session.rollback()
            raise DeploymentError(
                409, "environment_in_use", "배포한 앱과 진행 중인 작업을 먼저 정리하세요."
            )
        job = OnpremJob(
            id=identifier,
            environment_id=row.id,
            request_id=str(request_id),
            action=action,
            runtime_id=self.parent.runtime_id,
            created_at=int(time.time() * 1000),
        )
        session.add(job)
        if commit:
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return self.environment_job(session, row, request_id, action)
        return job, True

    def start_environment(self, job):
        try:
            self.parent.executor.submit(self.run_environment, job.id)
        except RuntimeError:
            self.finish_environment(
                job.id,
                {
                    "ok": False,
                    "error": {
                        "code": "worker_stopping",
                        "message": "실행기가 종료 중입니다. 다시 시도하세요.",
                        "retryable": True,
                    },
                },
            )

    def store(self, job_id, events):
        with self.sessions() as session:
            row = session.get(OnpremJob, job_id)
            if row is None or row.status not in ACTIVE:
                return
            extend(session, job_id)
            row.logs_json = json.dumps(
                add_events(json.loads(row.logs_json), events), ensure_ascii=False
            )
            session.commit()

    def run_environment(self, job_id):
        from .deploy_runner import LogSink

        sink = LogSink(lambda events: self.store(job_id, events))
        try:
            with self.sessions() as session:
                job = session.get(OnpremJob, job_id)
                if job is None or job.status not in ACTIVE:
                    return
                row = session.get(OnpremEnvironment, job.environment_id)
                if row.active_job_id != job.id:
                    return
                job.status = "running"
                session.commit()
                env = environment(
                    row, require_verified=False, cleanup=job.action == "remove_environment"
                )
                action = job.action
                if action == "remove_environment" and in_use(session, row.id):
                    raise DeploymentError(409, "environment_in_use", "배포한 앱을 먼저 정리하세요.")
            result = getattr(self.deployer, action)(env, sink.add, set_name="onprem")
            data = redact_model(result).model_dump(mode="json")
        except Exception as exc:
            data = {
                "ok": False,
                "error": {
                    "code": getattr(exc, "code", "environment_job_failed"),
                    "message": "환경 작업을 완료하지 못했습니다. 다시 시도하세요.",
                    "retryable": True,
                },
            }
        sink.flush()
        self.finish_environment(job_id, data)

    def finish_environment(self, job_id, data):
        now = int(time.time())
        with self.sessions() as session:
            job = session.get(OnpremJob, job_id)
            if job is None or job.status not in ACTIVE:
                return
            row = session.get(OnpremEnvironment, job.environment_id)
            # Lock and verify ownership before any state transition or selection cleanup.
            held = session.execute(
                update(OnpremEnvironment)
                .where(OnpremEnvironment.id == row.id, OnpremEnvironment.active_job_id == job_id)
                .values(lease_until=OnpremEnvironment.lease_until)
            )
            if held.rowcount != 1:
                session.rollback()
                return
            if data["ok"] and job.action == "check":
                try:
                    ip = verified_address(row, data)
                except (ValueError, DeployStateError):
                    # Never mark a callback address verified without a matching adapter observation.
                    data = {
                        "ok": False,
                        "error": {
                            "code": "public_ip_mismatch",
                            "message": "서버의 공인 IP를 확인하지 못했습니다.",
                            "retryable": True,
                        },
                    }
                else:
                    row.public_ip, row.last_seen_at, row.status = ip, now * 1000, "VERIFIED"
            if job.action == "check" and not data["ok"]:
                row.status = "SIGNALED"
            if data["ok"] and job.action == "remove_environment":
                session.execute(
                    delete(Deployment).where(Deployment.onprem_environment_id == row.id)
                )
                session.execute(
                    update(OnpremRegistrationToken)
                    .where(
                        OnpremRegistrationToken.environment_id == row.id,
                        OnpremRegistrationToken.used_at.is_(None),
                    )
                    .values(used_at=now)
                )
                row.deleted_at = now * 1000
            row.error_code = "" if data["ok"] else data["error"]["code"]
            job.status, job.finished_at = ("succeeded" if data["ok"] else "failed"), now * 1000
            job.result_json = json.dumps(
                present_result(row.connection_kind, data), ensure_ascii=False
            )
            release(session, job_id)
            session.commit()

    def submit_project(
        self, session, project, request_id, action, secrets=None, token=None, image_tag=""
    ):
        previous = find_request(session, project.id, request_id, action)
        if previous:
            if action == "rollback" and previous.image_tag != image_tag:
                raise DeploymentError(409, "request_conflict", "요청 ID의 롤백 버전이 다릅니다.")
            return previous, False
        target = session.get(Deployment, project.id)
        row = session.get(OnpremEnvironment, target.onprem_environment_id)
        environment(row)
        if action in ("destroy", "status", "rollback") and not target.app_name:
            raise DeploymentError(409, "not_deployed", "배포한 앱이 없습니다.")
        fetched = (
            self.parent.source.fetch(project.full_name, project.branch, token)
            if action == "deploy"
            else None
        )
        try:
            if fetched and target.app_name and target.app_name != str(fetched.spec.get("app", "")):
                raise DeploymentError(
                    409, "app_name_changed", "앱 이름을 바꾸려면 기존 배포를 먼저 제거하세요."
                )
            job, created = claim(
                session,
                project.id,
                request_id,
                self.parent.runtime_id,
                action=action,
                image_tag=fetched.commit_sha if fetched else image_tag,
            )
            if created:
                if fetched:
                    other = session.scalar(
                        select(Deployment.project_id).where(
                            Deployment.onprem_environment_id == row.id,
                            Deployment.project_id != project.id,
                            Deployment.app_name == str(fetched.spec.get("app", "")),
                        )
                    )
                    if other:
                        error = {
                            "code": "app_name_in_use",
                            "message": "이 환경의 다른 프로젝트가 같은 앱 이름을 사용 중입니다.",
                        }
                        finish(session, job.id, ok=False, result={"ok": False, "error": error})
                        raise DeploymentError(409, error["code"], error["message"])
                    # A failed or interrupted deploy may leave remote resources; keep its app name for cleanup.
                    target.app_name = str(fetched.spec.get("app", ""))
                    session.commit()
                self.parent._start(
                    session,
                    job,
                    self.run_project,
                    job.id,
                    row.id,
                    target.app_name,
                    fetched,
                    dict(secrets or {}),
                    cleanup=fetched,
                )
            else:
                self.parent._release(fetched)
            return job, created
        except BaseException:
            self.parent._release(fetched)
            raise

    def run_project(self, job_id, environment_id, app_name, fetched, secrets):
        from .deploy_runner import LogSink

        sink = LogSink(lambda events: self.parent._store(job_id, events), secrets)
        action, connection_kind = "deploy", "ssh"
        try:
            with self.sessions() as session:
                job = session.get(DeployJob, job_id)
                row = session.get(OnpremEnvironment, environment_id)
                if job is None or job.status not in ACTIVE or row.active_job_id != job_id:
                    return
                action, image_tag, connection_kind = job.action, job.image_tag, row.connection_kind
                job.status = "running"
                session.commit()
                env = environment(row)
            if action == "deploy":
                result = self.deployer.deploy(
                    env,
                    fetched.spec,
                    fetched.path,
                    fetched.commit_sha,
                    secrets,
                    sink.add,
                    set_name="onprem",
                )
            elif action == "rollback":
                result = self.deployer.rollback(
                    env, app_name, image_tag, sink.add, set_name="onprem"
                )
            else:
                result = getattr(self.deployer, action)(env, app_name, sink.add, set_name="onprem")
            data = present_result(
                connection_kind, redact_model(result, secrets).model_dump(mode="json")
            )
            deployed = None
            if result.ok and action in ("deploy", "rollback"):
                deployed = {
                    "image_tag": result.image_tag or image_tag,
                    "url": result.url or "",
                    "app_name": str(fetched.spec["app"]) if fetched else app_name,
                }
            elif result.ok and action == "destroy":
                deployed = {"image_tag": "", "url": "", "app_name": ""}
        except Exception:
            data, deployed = (
                {
                    "ok": False,
                    "error": {
                        "code": "deploy_runner_error",
                        "message": "배포 작업을 완료하지 못했습니다.",
                        "retryable": True,
                    },
                },
                None,
            )
        finally:
            sink.flush()
            self.parent._release(fetched)
        with self.sessions() as session:
            finish(
                session,
                job_id,
                ok=data["ok"],
                result=data,
                deployed=deployed,
                stage="" if data["ok"] else data.get("details", {}).get("stage", action),
            )

"""등록 작업과 프로젝트 배포 작업이 함께 쓰는 환경 단위 선점."""

import json
import time

from sqlalchemy import or_, select, update

from .db import DeployJob, Deployment, OnpremEnvironment, OnpremJob
from .deployments import ACTIVE, LEASE_SECONDS, DeploymentError


def acquire(session, environment_id, job_id, now=None):
    now = int(time.time()) if now is None else now
    changed = session.execute(
        update(OnpremEnvironment)
        .where(
            OnpremEnvironment.id == environment_id,
            OnpremEnvironment.deleted_at.is_(None),
            or_(OnpremEnvironment.active_job_id.is_(None), OnpremEnvironment.lease_until <= now),
        )
        .values(active_job_id=job_id, lease_until=now + LEASE_SECONDS)
    )
    if changed.rowcount != 1:
        session.rollback()
        raise DeploymentError(409, "environment_busy", "환경의 다른 작업이 진행 중입니다.")
    # 죽은 작업이 영원히 진행 중으로 보이거나, 나중에 새 소유자의 상태를 덮어쓰면 안 된다.
    expired = (
        select(DeployJob.id)
        .join(Deployment, Deployment.project_id == DeployJob.project_id)
        .where(
            Deployment.onprem_environment_id == environment_id,
            DeployJob.id != job_id,
            DeployJob.status.in_(ACTIVE),
            Deployment.lease_until <= now,
        )
    )
    ids = list(session.scalars(expired))
    failure = json.dumps(
        {"ok": False, "error": {"code": "lease_expired", "message": "작업이 중단되었습니다."}}
    )
    if ids:
        session.execute(
            update(DeployJob)
            .where(DeployJob.id.in_(ids))
            .values(status="interrupted", result_json=failure, finished_at=now * 1000)
        )
        session.execute(
            update(Deployment)
            .where(Deployment.active_job_id.in_(ids))
            .values(active_job_id=None, lease_until=0)
        )
    session.execute(
        update(OnpremJob)
        .where(
            OnpremJob.environment_id == environment_id,
            OnpremJob.id != job_id,
            OnpremJob.status.in_(ACTIVE),
        )
        .values(status="interrupted", result_json=failure, finished_at=now * 1000)
    )


def release(session, job_id):
    session.execute(
        update(OnpremEnvironment)
        .where(OnpremEnvironment.active_job_id == job_id)
        .values(active_job_id=None, lease_until=0)
    )


def extend(session, job_id):
    session.execute(
        update(OnpremEnvironment)
        .where(OnpremEnvironment.active_job_id == job_id)
        .values(lease_until=int(time.time()) + LEASE_SECONDS)
    )


def in_use(session, environment_id):
    """이 환경에 앱이 있거나 작업이 진행 중이면 True. 환경 이전 뒤 멈춘 채 남은 이전 앱도 포함한다."""
    current = select(Deployment.project_id).where(
        Deployment.onprem_environment_id == environment_id,
        or_(
            Deployment.app_name != "",
            Deployment.image_tag != "",
            Deployment.active_job_id.is_not(None),
        ),
    )
    left_behind = select(Deployment.project_id).where(
        Deployment.previous_kind == "onprem",
        Deployment.previous_environment_id == environment_id,
        Deployment.previous_app_name != "",
    )
    return session.scalar(current) is not None or session.scalar(left_behind) is not None

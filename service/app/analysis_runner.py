"""Service-side orchestration; only credential-free snapshots enter the AI child."""

import json
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import analyses
from .ai_provider import ProcessAIProvider
from .analysis_source import AnalysisError, fetch_snapshot
from .db import AnalysisRun, AnalysisSlot, Project
from .deploy_runner import _lock
from .github_api import GitHubFailure


class AnalysisRunner:
    def __init__(self, settings, sessions, github, *, provider=None, source=fetch_snapshot):
        self.settings, self.sessions, self.github = settings, sessions, github
        self.provider, self.source = provider or ProcessAIProvider(settings), source
        self.runtime_id = str(uuid.uuid4())
        self.executor = self.lock_file = None
        self.stopping = threading.Event()
        self.capacity = threading.BoundedSemaphore(4)

    def start(self):
        self.lock_file = _lock(self.settings.ai_workspace / "locks", self.settings.database_url)
        try:
            with self.sessions() as session:
                analyses.interrupt(session)
            self.stopping.clear()
            self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="anyship-ai")
        except BaseException:
            self.lock_file.close()
            raise

    def close(self):
        self.stopping.set()
        if self.executor:
            # Children observe the event and terminate, then release their temporary dirs.
            self.executor.shutdown(wait=True, cancel_futures=False)
            self.executor = None
            with self.sessions() as session:
                analyses.interrupt(session, self.runtime_id)
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def submit(self, session, project, request, token):
        previous = session.scalar(
            select(AnalysisRun).where(
                AnalysisRun.project_id == project.id, AnalysisRun.request_id == request.request_id
            )
        )
        if previous:
            if (
                previous.target_env != request.target_env
                or previous.provider != self.settings.ai_provider
            ):
                raise AnalysisError("request_conflict", "동일 요청 식별자의 설정이 다릅니다.")
            return previous, False
        if (
            self.executor is None
            or self.stopping.is_set()
            or not self.capacity.acquire(blocking=False)
        ):
            raise AnalysisError(
                "analysis_busy", "분석 작업이 많습니다. 잠시 후 다시 요청해 주세요."
            )
        try:
            # Serialize submission with project deletion on PostgreSQL.
            if (
                session.scalar(select(Project).where(Project.id == project.id).with_for_update())
                is None
            ):
                raise AnalysisError("project_missing", "프로젝트 연결이 삭제되었습니다.")
            previous = session.scalar(
                select(AnalysisRun).where(
                    AnalysisRun.project_id == project.id,
                    AnalysisRun.request_id == request.request_id,
                )
            )
            if previous:
                if (
                    previous.target_env != request.target_env
                    or previous.provider != self.settings.ai_provider
                ):
                    raise AnalysisError("request_conflict", "동일 요청 식별자의 설정이 다릅니다.")
                self.capacity.release()
                return previous, False
            base = self.github.branch(token, project.full_name, project.branch)["commit"]["sha"]
            if session.get(AnalysisSlot, project.id) is None:
                session.add(AnalysisSlot(project_id=project.id))
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()
            now, identifier = int(time.time()), str(uuid.uuid4())
            claimed = session.execute(
                update(AnalysisSlot)
                .where(AnalysisSlot.project_id == project.id, AnalysisSlot.lease_until <= now)
                .values(
                    active_id=identifier, lease_until=now + 2 * (self.settings.ai_timeout + 180)
                )
            )
            if claimed.rowcount != 1:
                session.rollback()
                raise AnalysisError("analysis_busy", "이 프로젝트의 분석을 처리 중입니다.")
            session.execute(
                update(AnalysisRun)
                .where(
                    AnalysisRun.project_id == project.id, AnalysisRun.status.in_(analyses.ACTIVE)
                )
                .values(
                    status="interrupted",
                    finished_at=now,
                    error_code="lease_expired",
                    error="이전 작업이 중단됐습니다.",
                )
            )
            row = AnalysisRun(
                id=identifier,
                project_id=project.id,
                request_id=request.request_id,
                runtime_id=self.runtime_id,
                repository_id=project.repository_id,
                repository=project.full_name,
                base_branch=project.branch,
                base_sha=base,
                provider=self.settings.ai_provider,
                target_env=request.target_env,
                branch="anyship/analysis-" + identifier.replace("-", ""),
                created_at=now,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                previous = session.scalar(
                    select(AnalysisRun).where(
                        AnalysisRun.project_id == project.id,
                        AnalysisRun.request_id == request.request_id,
                    )
                )
                if previous:
                    self.capacity.release()
                    return previous, False
                raise
            try:
                self.executor.submit(self._run, row.id, token)
            except RuntimeError:
                self._finish(
                    row.id,
                    status="interrupted",
                    error_code="worker_interrupted",
                    error="서버 종료로 작업을 시작하지 못했습니다.",
                )
                raise AnalysisError(
                    "worker_interrupted", "서버 종료로 작업을 시작하지 못했습니다."
                ) from None
            return row, True
        except BaseException:
            self.capacity.release()
            raise

    def _event(self, identifier, stage):
        with self.sessions() as session:
            row = session.get(AnalysisRun, identifier)
            if row and row.status in analyses.ACTIVE:
                events = json.loads(row.logs_json)
                if not events or events[-1]["stage"] != stage:
                    events.append({"stage": stage, "at": int(time.time())})
                    session.execute(
                        update(AnalysisRun)
                        .where(
                            AnalysisRun.id == identifier, AnalysisRun.status.in_(analyses.ACTIVE)
                        )
                        .values(logs_json=analyses.encoded(events[-100:]))
                    )
                    session.commit()

    def _finish(self, identifier, **values):
        with self.sessions() as session:
            row = session.get(AnalysisRun, identifier)
            if not row:
                return
            slot = session.get(AnalysisSlot, row.project_id)
            if not slot or slot.active_id != identifier:
                return
            session.execute(
                update(AnalysisRun)
                .where(
                    AnalysisRun.id == identifier,
                    AnalysisRun.runtime_id == self.runtime_id,
                    AnalysisRun.status.in_(analyses.ACTIVE),
                )
                .values(**values, finished_at=int(time.time()))
            )
            session.execute(
                update(AnalysisSlot)
                .where(
                    AnalysisSlot.project_id == row.project_id, AnalysisSlot.active_id == identifier
                )
                .values(active_id=None, lease_until=0)
            )
            session.commit()

    def _run(self, identifier, token):
        try:
            with self.sessions() as session:
                row = session.get(AnalysisRun, identifier)
                if self.stopping.is_set() or row.status != "queued":
                    raise AnalysisError("worker_interrupted", "서버 종료로 분석이 중단됐습니다.")
                session.execute(
                    update(AnalysisRun)
                    .where(AnalysisRun.id == identifier, AnalysisRun.status == "queued")
                    .values(status="running")
                )
                session.commit()
                request = {
                    "base_sha": row.base_sha,
                    "source_repo": "https://github.com/" + row.repository,
                    "app_name": "app-" + row.project_id.replace("-", "")[:20],
                    "provider": row.provider,
                    "target_env": row.target_env,
                    "max_calls": self.settings.ai_max_calls,
                }
            self._event(identifier, "소스 준비 중")
            self.settings.ai_workspace.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="job-", dir=self.settings.ai_workspace
            ) as temporary:
                snapshot = self.source(
                    self.github,
                    token,
                    row.repository,
                    row.base_sha,
                    Path(temporary) / "repo",
                    cancelled=self.stopping.is_set,
                )
                token = None  # The child receives only a snapshot and non-secret request metadata.
                result = self.provider.run(
                    Path(temporary),
                    request,
                    lambda stage: self._event(identifier, stage),
                    self.stopping.is_set,
                )
                files, diff = analyses.validated_bundle(snapshot, result)
                result.report["source_skipped_files"] = snapshot.skipped
                row.base_tree, row.files_json, row.diff = (
                    snapshot.base_tree,
                    analyses.encoded(files),
                    diff,
                )
                row.report_json = analyses.encoded(result.report)
                row.review_hash = analyses.review_hash(row) if files else ""
                self._finish(
                    identifier,
                    status="completed",
                    base_tree=row.base_tree,
                    files_json=row.files_json,
                    diff=diff,
                    report_json=row.report_json,
                    review_hash=row.review_hash,
                )
        except AnalysisError as error:
            self._finish(
                identifier,
                status="interrupted" if self.stopping.is_set() else "failed",
                error_code=error.code,
                error=error.message,
            )
        except GitHubFailure:
            self._finish(
                identifier,
                status="failed",
                error_code="source_access_failed",
                error="GitHub 소스 접근에 실패했습니다. 권한과 인증을 확인해 주세요.",
            )
        except Exception:
            self._finish(
                identifier,
                status="failed",
                error_code="analysis_failed",
                error="분석 결과를 처리하지 못했습니다. 설정과 입력 지원 범위를 확인해 주세요.",
            )
        finally:
            self.capacity.release()

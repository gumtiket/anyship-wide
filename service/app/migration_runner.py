"""환경 이전(`migrate`)과 이전 앱 삭제(`destroy_previous`). 배포·삭제와 같은 선점, 멱등, 로그 저장 규칙을 쓴다.

`migrate`는 프로젝트의 배포 대상을 **다른 환경으로 옮긴다.** 한 작업 안에서 차례로 한다.
  1. 원본 확인: 원본 서버와 앱, DB 크기(한도). 대상을 새로 배포하기 *전에* 한다(옮길 수 없는 DB 때문에 20분짜리 배포를 헛하지 않게).
  2. 대상에 같은 앱을 새로 배포: 지금 브랜치의 최신 커밋을 받아 대상 환경에 배포한다(빈 DB). 사용자 비밀은 다시 입력받는다(저장하지 않는다).
  3. DB 이전: 원본의 쓰기를 멈추고 덤프를 대상으로 흘려보낸 뒤 행 수를 검증한다(`Deployer.transfer_data`).
  4. 대상 교체: 성공하면 프로젝트의 배포 대상을 새 환경으로 바꾸고, 원래 환경의 **멈춘 앱**을 `previous_*`에 기록한다.
성공 뒤 원래 앱은 멈춘 채 남는다. 사용자가 새 주소에서 확인한 뒤 `destroy_previous`로 지운다(그때까지 서비스는 이 앱을 기억한다).

실패하면 배포 대상은 그대로이고 원본 앱은 다시 시작한다. 단 2단계가 이미 대상에 앱을 올렸다면 그 앱은 대상에 남는다
(다시 옮기면 그 위에 다시 배포하고 DB를 덮어쓴다. 서비스가 추적하는 앱이 아니므로 환경 정리 전에 직접 지워야 할 수 있다).

비밀: 사용자가 입력한 비밀은 메모리로만 넘기고 로그와 결과에서 가린다. GitHub 토큰은 요청 안에서 소스를 받을 때만 쓴다.
"""

from sqlalchemy import select

from anyship_adapters import AdapterError, DeployResult, DestroyResult, redact_model
from anyship_adapters.aws_access import AwsAccessError

from .db import AwsEnvironment, DeployJob, Deployment, OnpremEnvironment
from .deploy_state import (
    DeployStateError,
    adapter_environment,
    assign_env_id,
    ensure_state_bucket,
    save_foundation,
)
from .deployments import ACTIVE, DeploymentError, claim, find_request, finish
from .onprem_transport import environment as onprem_environment

SETS = {"aws": "aws-always-on", "onprem": "onprem"}


def kind_of(row) -> str:
    return "onprem" if isinstance(row, OnpremEnvironment) else "aws"


def failure(
    code: str,
    message: str,
    *,
    hint: str | None = None,
    retryable: bool = True,
    details: dict | None = None,
) -> dict:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "hint": hint, "retryable": retryable},
        "details": details or {},
    }


class MigrationRunner:
    def __init__(self, parent, deployer):
        """`deployer`는 AWS와 온프레미스 어댑터를 모두 가진 `Deployer`다(데이터 이전과 이전 앱 삭제에 쓴다).
        대상 환경에 새로 배포하는 일은 부모의 기존 배포기(AWS, 온프레미스)가 한다."""
        self.parent, self.deployer, self.sessions = parent, deployer, parent.sessions

    # -- 요청 안에서 하는 일 --------------------------------------------------------------------
    def _environment_row(self, session, kind: str, identifier: str):
        return session.get(OnpremEnvironment if kind == "onprem" else AwsEnvironment, identifier)

    def _source(self, session, target: Deployment):
        kind = "onprem" if target.onprem_environment_id else "aws"
        return kind, self._environment_row(
            session, kind, target.onprem_environment_id or target.aws_environment_id
        )

    def _adapter_environment(self, session, row):
        """어댑터에 넘길 환경. 연결이 확인되지 않은 환경은 `DeployStateError`로 거절한다."""
        if isinstance(row, OnpremEnvironment):
            return onprem_environment(row)
        ensure_state_bucket(session, row, self.parent.access or _default_access())
        return adapter_environment(row)

    def submit_migrate(
        self, session, project, request_id, destination, secrets: dict, token: str | None
    ):
        """원본(현재 배포 대상)의 앱을 `destination` 환경으로 옮기는 작업을 선점해 스레드에 넘긴다. (작업, 새로 만들었는지)."""
        if previous := find_request(session, project.id, request_id, "migrate"):
            return previous, False  # 같은 요청의 재전송은 소스를 다시 받지 않는다
        target = session.get(Deployment, project.id)
        if target is None or not target.image_tag or not target.app_name:
            raise DeploymentError(
                409, "not_deployed", "배포된 앱이 있어야 다른 환경으로 옮길 수 있습니다."
            )
        source_kind, source = self._source(session, target)
        destination_kind = kind_of(destination)
        if (source_kind, source.id) == (destination_kind, destination.id):
            raise DeploymentError(
                422, "same_environment", "지금 배포한 환경과 같은 환경으로는 옮길 수 없습니다."
            )
        if destination_kind == "aws":
            assign_env_id(session, destination)
        try:  # 연결이 확인되지 않은 환경은 작업을 만들기 전에 거절한다
            adapter_environment(source) if source_kind == "aws" else onprem_environment(source)
            adapter_environment(destination) if destination_kind == "aws" else onprem_environment(
                destination
            )
        except DeployStateError as error:
            raise DeploymentError(409, error.code, error.message) from None
        fetched = self.parent.source.fetch(project.full_name, project.branch, token)
        try:
            app = str(fetched.spec.get("app", ""))
            if app != target.app_name:
                raise DeploymentError(
                    409,
                    "app_name_changed",
                    "배포 명세의 앱 이름이 지금 배포한 앱과 다릅니다. 이름을 바꾸려면 기존 배포를 먼저 제거하세요.",
                )
            if self._app_name_in_use(session, destination_kind, destination.id, app, project.id):
                raise DeploymentError(
                    409,
                    "app_name_in_use",
                    "대상 환경의 다른 프로젝트가 같은 앱 이름을 사용 중입니다.",
                )
            job, created = claim(
                session,
                project.id,
                request_id,
                self.parent.runtime_id,
                action="migrate",
                image_tag=fetched.commit_sha,
                extra_onprem_environment_ids=(destination.id,)
                if destination_kind == "onprem"
                else (),
            )
        except BaseException:
            self.parent._release(fetched)
            raise
        if created:
            self.parent._start(
                session,
                job,
                self.run_migrate,
                job.id,
                source_kind,
                source.id,
                destination_kind,
                destination.id,
                fetched,
                dict(secrets),
                cleanup=fetched,
            )
        else:
            self.parent._release(fetched)  # 동시에 들어온 같은 요청이 먼저 만들었다
        return job, created

    @staticmethod
    def _app_name_in_use(
        session, kind: str, environment_id: str, app: str, project_id: str
    ) -> bool:
        column = (
            Deployment.onprem_environment_id if kind == "onprem" else Deployment.aws_environment_id
        )
        current = select(Deployment.project_id).where(
            column == environment_id,
            Deployment.app_name == app,
            Deployment.project_id != project_id,
        )
        left_behind = select(Deployment.project_id).where(
            Deployment.previous_kind == kind,
            Deployment.previous_environment_id == environment_id,
            Deployment.previous_app_name == app,
            Deployment.project_id != project_id,
        )
        return session.scalar(current) is not None or session.scalar(left_behind) is not None

    def submit_destroy_previous(self, session, project, request_id):
        """환경 이전 뒤 원래 환경에 멈춘 채 남은 앱을 지우는 작업을 선점해 스레드에 넘긴다. (작업, 새로 만들었는지)."""
        if previous := find_request(session, project.id, request_id, "destroy_previous"):
            return previous, False
        target = session.get(Deployment, project.id)
        if target is None or not target.previous_app_name:
            raise DeploymentError(409, "no_previous_app", "지울 이전 환경의 앱이 없습니다.")
        kind, identifier = target.previous_kind, target.previous_environment_id
        row = self._environment_row(session, kind, identifier)
        if row is None:
            raise DeploymentError(409, "environment_unavailable", "이전 환경을 찾을 수 없습니다.")
        try:
            adapter_environment(row) if kind == "aws" else onprem_environment(row)
        except DeployStateError as error:
            raise DeploymentError(409, error.code, error.message) from None
        job, created = claim(
            session,
            project.id,
            request_id,
            self.parent.runtime_id,
            action="destroy_previous",
            extra_onprem_environment_ids=(identifier,) if kind == "onprem" else (),
        )
        if created:
            self.parent._start(
                session,
                job,
                self.run_destroy_previous,
                job.id,
                kind,
                identifier,
                target.previous_app_name,
            )
        return job, created

    # -- 스레드 안에서 하는 일 ------------------------------------------------------------------
    def run_migrate(
        self, job_id, source_kind, source_id, destination_kind, destination_id, fetched, secrets
    ):
        try:
            self._migrate(
                job_id, source_kind, source_id, destination_kind, destination_id, fetched, secrets
            )
        finally:
            self.parent._release(fetched)  # 어떤 경우에도 받아 둔 임시 소스를 남기지 않는다

    def _migrate(
        self, job_id, source_kind, source_id, destination_kind, destination_id, fetched, secrets
    ):
        from .deploy_runner import LogSink

        known = dict(secrets)
        sink = LogSink(lambda events: self.parent._store(job_id, events), known)
        stage, changes, deployed, data = "runner", None, None, None
        try:
            with self.sessions() as session:
                job = session.get(DeployJob, job_id)
                if job is None or job.status not in ACTIVE:
                    return
                job.status = "running"
                session.commit()
                source, destination = (
                    self._environment_row(session, source_kind, source_id),
                    self._environment_row(session, destination_kind, destination_id),
                )
                old_url = session.get(Deployment, job.project_id).url
                try:
                    source_env = self._adapter_environment(session, source)
                    destination_env = self._adapter_environment(session, destination)
                except (DeployStateError, AwsAccessError) as exc:
                    error = getattr(exc, "error", None)
                    code = error.code if error else getattr(exc, "code", "environment_unavailable")
                    message = (
                        error.message
                        if error
                        else getattr(exc, "message", "환경 정보를 준비하지 못했습니다.")
                    )
                    data, stage = failure(code, message, retryable=False), "environment"
            if data is None:
                app, source_set, destination_set = (
                    str(fetched.spec["app"]),
                    SETS[source_kind],
                    SETS[destination_kind],
                )
                checked = self.deployer.check_transfer_source(
                    source_env, app, sink.add, set_name=source_set
                )
                if not checked.ok:
                    data, stage = redact_model(checked, known).model_dump(mode="json"), "preflight"
            if data is None:
                builder = (
                    self.parent.deployer
                    if destination_kind == "aws"
                    else self.parent.onprem.deployer
                )
                deployed_result = builder.deploy(
                    destination_env,
                    fetched.spec,
                    fetched.path,
                    fetched.commit_sha,
                    secrets,
                    sink.add,
                    set_name=destination_set,
                )
                if not deployed_result.ok:
                    data = redact_model(deployed_result, known).model_dump(mode="json")
                    stage = str((deployed_result.details or {}).get("stage", "deploy"))
                    data["details"] = {**(data.get("details") or {}), "target_left": False}
                else:
                    foundation = (deployed_result.details or {}).get("foundation")
                    if foundation and destination_kind == "aws":
                        with self.sessions() as session:
                            try:
                                save_foundation(
                                    session, session.get(AwsEnvironment, destination_id), foundation
                                )
                            except DeployStateError:
                                pass  # 배포는 성공했다. 기반 값은 다음 배포가 state에서 다시 읽는다
            if data is None:
                moved = self.deployer.transfer_data(
                    source_env,
                    destination_env,
                    app,
                    sink.add,
                    source_set=source_set,
                    target_set=destination_set,
                )
                if not moved.ok:
                    data = redact_model(moved, known).model_dump(mode="json")
                    data["details"] = {**(data.get("details") or {}), "target_left": True}
                    stage = "transfer"
                else:
                    url = deployed_result.url or ""
                    result = DeployResult(
                        ok=True,
                        url=url,
                        image_tag=deployed_result.image_tag or fetched.commit_sha,
                        details={**moved.details, "migrated_from": source_kind, "stopped_app": app},
                    )
                    data = redact_model(result, known).model_dump(mode="json")
                    deployed = {"image_tag": result.image_tag, "url": url, "app_name": app}
                    changes = {
                        "aws_environment_id": destination_id if destination_kind == "aws" else None,
                        "onprem_environment_id": destination_id
                        if destination_kind == "onprem"
                        else None,
                        "set_name": destination_set,
                        "previous_kind": source_kind,
                        "previous_environment_id": source_id,
                        "previous_app_name": app,
                        "previous_url": old_url,
                    }
                    stage = ""
        except Exception as exc:
            # 예기치 않은 예외의 원문에는 비밀이 섞일 수 있다. 종류만 남긴다.
            data = failure(
                "migration_runner_error",
                "환경 이전 중 예기치 않은 오류가 발생했습니다.",
                hint="서비스 서버의 로그를 확인해 주세요.",
                details={"exception": type(exc).__name__},
            )
            stage, deployed, changes = "runner", None, None
        sink.flush()
        with self.sessions() as session:
            finish(
                session,
                job_id,
                ok=data["ok"],
                result=data,
                deployed=deployed,
                changes=changes,
                stage="" if data["ok"] else stage,
            )

    def run_destroy_previous(self, job_id, kind, environment_id, app_name):
        from .deploy_runner import LogSink

        sink = LogSink(lambda events: self.parent._store(job_id, events))
        stage = "destroy"
        try:
            with self.sessions() as session:
                job = session.get(DeployJob, job_id)
                if job is None or job.status not in ACTIVE:
                    return
                job.status = "running"
                session.commit()
                row = self._environment_row(session, kind, environment_id)
                try:
                    env = self._adapter_environment(session, row)
                    result = None
                except (DeployStateError, AwsAccessError) as exc:
                    error = getattr(exc, "error", None)
                    stage = "environment"
                    result = DestroyResult(
                        ok=False,
                        error=error
                        or AdapterError(
                            code=getattr(exc, "code", "environment_unavailable"),
                            message=getattr(exc, "message", "환경 정보를 준비하지 못했습니다."),
                        ),
                    )
            if result is None:
                result = self.deployer.destroy(env, app_name, sink.add, set_name=SETS[kind])
        except Exception as exc:
            stage = "runner"
            result = DestroyResult(
                ok=False,
                error=AdapterError(
                    code="migration_runner_error",
                    message="이전 앱을 지우는 중 예기치 않은 오류가 발생했습니다.",
                    hint="서비스 서버의 로그를 확인해 주세요.",
                    retryable=True,
                ),
                details={"exception": type(exc).__name__},
            )
        sink.flush()
        cleared = {
            "previous_kind": "",
            "previous_environment_id": "",
            "previous_app_name": "",
            "previous_url": "",
        }
        with self.sessions() as session:
            finish(
                session,
                job_id,
                ok=result.ok,
                result=redact_model(result).model_dump(mode="json"),
                stage="" if result.ok else stage,
                changes=cleared if result.ok else None,
            )


def _default_access():
    from anyship_adapters.aws_access import AwsAccess

    return AwsAccess()

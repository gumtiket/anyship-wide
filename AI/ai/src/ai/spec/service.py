import json
from importlib.resources import files
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from ai.detectors.repo import RepoView
from ai.llm.base import LLMClient
from ai.models import Diagnosis, TransformReport, WarningItem
from ai.security import credential_name
from ai.spec.env_policy import EXTERNAL_URLS, allowed_name
from ai.spec.models import (
    BackingService,
    DeploySpec,
    Environment,
    Release,
    Source,
    SpecProposal,
    Workload,
)
from ai.spec.resources import resolve_resources


def validate_spec(spec: DeploySpec) -> None:
    DeploySpec.model_validate(spec.model_dump(mode="json"))  # 바이트 합계와 의미 검사를 포함한다.
    schema = json.loads(files("ai").joinpath("spec/schema.json").read_text())
    Draft202012Validator(schema).validate(spec.model_dump(mode="json", exclude_none=True))


def generate_spec(
    app: str,
    source_repo: str,
    commit: str | None,
    profile: Literal["dev", "prod"],
    diagnosis: Diagnosis,
    transformation: TransformReport,
    llm: LLMClient | None,
    *,
    max_request_seconds: int = 10,
    repo: RepoView | None = None,
) -> tuple[DeploySpec, bool]:
    proposal = SpecProposal()
    fallback = False
    if llm is not None:
        try:
            response = llm.complete(
                files("ai").joinpath("prompts/spec.txt").read_text(),
                json.dumps({"signals": [s.model_dump(mode="json") for s in diagnosis.signals]}),
                tier="fast",
                schema=SpecProposal,
                stage="spec",
            )
            if not isinstance(response.parsed, SpecProposal):
                raise ValueError("spec_proposal_invalid")
            proposal = response.parsed
        except (ValueError, RuntimeError):
            fallback = True
    websocket = any(signal.name == "websocket" for signal in diagnosis.signals)
    persistent = websocket or any(signal.name == "scheduler" for signal in diagnosis.signals)
    if (
        proposal.max_request_seconds != max_request_seconds
        or proposal.ingress != "public"
        or proposal.scale_to_zero != (not persistent)
    ):
        fallback = True
    postgres, migrate = resolve_resources(
        repo,
        transformation,
        converted_postgres=any(
            v.rule == "sqlite_usage" and v.id in transformation.addressed_ids
            for v in diagnosis.violations
        ),
    )
    env = []
    for variable in transformation.env_vars:
        if variable.name == "PORT":
            continue
        if postgres and variable.name == "DATABASE_URL":
            continue  # 백엔드 서비스 URL은 어댑터가 주입한다. 임의의 DB URL을 생성하면 안 된다.
        if not allowed_name(variable.name):
            transformation.warnings.append(
                WarningItem(
                    code="environment_name_forbidden",
                    message=f"{variable.name}: 플랫폼/프로세스 환경변수는 앱 명세로 주입하지 "
                    "않습니다.",
                )
            )
            if variable.name in EXTERNAL_URLS:
                transformation.warnings.append(
                    WarningItem(
                        code="external_resource_required",
                        message=f"{variable.name}: C의 외부 자원 바인딩 확인이 필요합니다.",
                    )
                )
            continue
        secret = variable.secret or credential_name(variable.name)
        if not secret and variable.default is None:
            transformation.warnings.append(
                WarningItem(
                    code="environment_value_required",
                    message=f"{variable.name}: 사용자 설정 값 확인이 필요합니다.",
                )
            )
            continue
        if variable.name in EXTERNAL_URLS:
            transformation.warnings.append(
                WarningItem(
                    code="external_resource_required",
                    message=f"{variable.name}: 기존 외부 자원 주소를 사용자에게 받아야 합니다. "
                    "자원을 임의 생성하지 않습니다.",
                )
            )
        if variable.name == "STORAGE_URL":
            transformation.warnings.append(
                WarningItem(
                    code="object_storage_mvp_unsupported",
                    message="MVP는 object_storage 배포를 지원하지 않습니다. "
                    "기존 스토리지 사용은 추가 검토가 필요합니다.",
                )
            )
        try:
            setting = Environment(
                name=variable.name,
                secret=secret,
                generate=secret and variable.name == "SECRET_KEY" and variable.generate,
                value=None if secret else variable.default,
            )
        except ValidationError:
            transformation.warnings.append(
                WarningItem(
                    code="environment_value_unsafe",
                    message=f"{variable.name}: 앱 설정 값의 형식/크기 확인이 필요합니다. "
                    "원문은 출력하지 않습니다.",
                )
            )
            continue
        env.append(setting)
    release = None
    if migrate:
        try:
            release = Release(migrate=migrate)
        except ValidationError:
            transformation.warnings.append(
                WarningItem(
                    code="release_migrate_deferred",
                    message="migrate 명령은 한 줄 500자 이하만 허용합니다. 생성을 보류했습니다.",
                )
            )
            transformation.migrate_command = None
    spec = DeploySpec(
        app=app,
        source=Source(repo=source_repo, commit=commit),
        env=env,
        backing_services=[BackingService(type="postgres", bind_as="DATABASE_URL")]
        if postgres
        else [],
        workload=Workload(
            type="always-on" if persistent else "request-driven",
            scale_to_zero=not persistent,
            max_request_seconds=max_request_seconds,
            websocket=websocket,
        ),
        ingress="public",
        release=release,
        profile=profile,
    )
    validate_spec(spec)
    return spec, fallback

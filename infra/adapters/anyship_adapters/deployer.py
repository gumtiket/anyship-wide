"""배포 파이프라인: 소스 폴더에서 공개 URL까지 한 번의 호출로 잇는다.

서비스(A)는 이 호출 하나만 알면 된다. 순서는 이렇다.

    이미지 빌드 -> (aws 세트) 공용 기반 확인(+ DNS 레코드) -> 연결 확인 -> 배포 -> 오래된 이미지 정리

공용 기반은 이미 있으면 state의 출력만 읽고(약 10초), 없으면 만든다(첫 배포만, 약 20분). 서비스가 저장해 둔 기반 값이 있어도 매번
읽는 것은 호스트가 바뀌었을 때 낡은 값을 쓰지 않으려는 것이다(비용은 약 10초).

`dns`(범위 `aws`의 `WildcardRecords`)를 주면 기반의 호스트 IP를 읽은 직후에 `*.<환경ID>.aws.<도메인>`을 그 IP로 맞춘다. 인증서 발급과
헬스체크가 이 주소로 오므로 연결 확인 전에 해야 하고, 매 배포마다 맞추므로 호스트가 바뀌어도 스스로 고쳐진다. 주지 않으면 건너뛴다(수동 DNS).

입력 오류(명세, 비밀 누락, SHA, 세트와 환경의 불일치)는 **아무것도 시작하기 전에** 찾는다. 20분짜리 기반 생성 뒤에야 "비밀이 빠졌다"를
알게 되는 일을 막으려는 것이다.

각 부품의 로그는 큰 단계(1/5 ~ 5/5)로 다시 이름 붙여서 흘려 보낸다. 부품 안쪽의 단계 번호는 메시지 앞에 `[3/8]`로 남는다.
실패는 예외가 아니라 `ok=False` 결과로 돌려주고, `details["stage"]`가 실패한 단계(spec, build, foundation, check, deploy)를 알려 준다.
"""

import re
from collections.abc import Mapping
from typing import Any

from .base import Adapter, LogFn
from .compose import render_stack
from .data_transfer import DEFAULT_MAX_BYTES, TransferInputError, inspect_source, transfer_database
from .dns import DnsError, WildcardRecords
from .foundation import FoundationSettings, ensure_foundation
from .image_builder import BuildError, ImageBuilder
from .models import (
    APP_NAME_PATTERN,
    IMAGE_TAG_PATTERN,
    AdapterError,
    CheckResult,
    DeployResult,
    DestroyResult,
    Environment,
    LogEvent,
    Secrets,
    Spec,
    StatusResult,
    TransferResult,
)
from .rds_admin import db_name
from .redact import make_safe_log, redact_json, redact_model
from .sets import AWS_ALWAYS_ON, AWS_SERVERLESS, ONPREM, SetName
from .spec import SpecError, parse_spec
from .terraform_runner import TerraformError, TerraformRunner

_TAG = re.compile(IMAGE_TAG_PATTERN)
_APP = re.compile(APP_NAME_PATTERN)
SET_KINDS = {
    AWS_ALWAYS_ON: "aws",
    AWS_SERVERLESS: "aws",
    ONPREM: "onprem",
}  # 세트가 쓸 수 있는 환경 종류
_STAGES = {
    "aws": (
        ("build", "이미지 빌드"),
        ("foundation", "공용 기반 확인"),
        ("check", "연결 확인"),
        ("deploy", "배포"),
        ("cleanup", "정리"),
    ),
    "onprem": (
        ("build", "이미지 빌드"),
        ("check", "연결 확인"),
        ("deploy", "배포"),
        ("cleanup", "정리"),
    ),
}


class Deployer:
    def __init__(
        self,
        adapters: Mapping[str, Adapter],
        builder: ImageBuilder,
        *,
        runner: TerraformRunner | None = None,
        foundation: FoundationSettings | None = None,
        dns: WildcardRecords | None = None,
        keep_images: int = 5,
    ):
        if (runner is None) != (foundation is None):
            raise ValueError("runner and foundation settings must be given together")
        if dns is not None and runner is None:
            raise ValueError(
                "dns needs the foundation step (runner and foundation settings)"
            )  # 기반 단계가 없으면 호스트 IP를 알 수 없다
        self._adapters, self._builder, self._runner, self._foundation, self._keep = (
            adapters,
            builder,
            runner,
            foundation,
            keep_images,
        )
        self._dns = dns

    def deploy(
        self,
        env: Environment,
        spec: Spec,
        source_dir,
        commit_sha: str,
        secrets: Secrets,
        log: LogFn,
        *,
        set_name: SetName,
    ) -> DeployResult:
        known = dict(secrets)
        safe = make_safe_log(log, known)

        def fail(
            error: AdapterError, stage: str, details: Mapping[str, Any] | None = None
        ) -> DeployResult:
            error = redact_model(error, known)
            safe(LogEvent(level="error", message=error.message))
            return DeployResult(
                ok=False,
                error=error,
                image_tag=commit_sha if _TAG.match(commit_sha) else None,
                details=redact_json({**(details or {}), "stage": stage}, known),
            )

        # 1) 시작하기 전에 입력부터 거른다
        kind, adapter = SET_KINDS.get(set_name), self._adapters.get(set_name)
        if adapter is None or kind != env.kind:
            return fail(
                AdapterError(
                    code="set_not_supported",
                    message=f"이 환경에서는 '{set_name}' 세트를 사용할 수 없습니다.",
                ),
                "spec",
            )
        try:
            parsed = parse_spec(spec)
            render_stack(
                spec,
                host=f"{parsed.app}.{env.env_id}.preflight.example",
                image_tag=commit_sha,
                secrets=secrets,
            )
            if kind == "aws":
                db_name(
                    parsed.app
                )  # 앱 이름이 DB 식별자 한도에 맞는지(어댑터가 20분 뒤에 알려 주지 않게)
        except SpecError as exc:
            return fail(exc.error, "spec")
        except ValueError:
            return fail(
                AdapterError(
                    code="invalid_spec",
                    message="앱 이름이 너무 길어 DB 이름을 만들 수 없습니다.",
                    hint="앱 이름을 59자 이하로 줄여 주세요.",
                ),
                "spec",
            )

        stages = [s for s in _STAGES[kind] if not (s[0] == "foundation" and self._runner is None)]
        current = "build"

        def relay(key: str) -> LogFn:
            index = next(i for i, (k, _) in enumerate(stages) if k == key)
            name = stages[index][1]

            def forward(event: LogEvent) -> None:
                prefix = f"[{event.step}/{event.total}] " if event.step and event.total else ""
                safe(
                    LogEvent(
                        level=event.level,
                        step=index + 1,
                        total=len(stages),
                        name=name,
                        message=(prefix + event.message)[:2000],
                    )
                )

            return forward

        try:
            build = spec.get("build")
            dockerfile = (
                build.get("dockerfile", "Dockerfile")
                if isinstance(build, Mapping)
                else "Dockerfile"
            )
            built = self._builder.build(
                source_dir, parsed.app, commit_sha, relay("build"), dockerfile=dockerfile
            )

            foundation_created, dns_record = None, None
            if self._runner is not None and kind == "aws":
                current = "foundation"
                ensured = ensure_foundation(
                    self._runner, env, self._foundation, relay("foundation")
                )
                env, foundation_created = ensured.env, ensured.created
                if self._dns is not None:
                    dns_record = self._ensure_dns(env, relay("foundation"))

            current = "check"
            checked = adapter.check(env, relay("check"))
            if not checked.ok:
                return fail(checked.error, "check", checked.details)

            current = "deploy"
            result = adapter.deploy(
                env, spec, commit_sha, secrets, relay("deploy"), set_name=set_name
            )
            if not result.ok:
                return fail(result.error, "deploy", result.details)

            current = "cleanup"
            cleanup = relay("cleanup")
            try:  # 정리는 배포의 성공 여부를 바꾸지 않는다
                pruned = self._builder.prune(parsed.app, self._keep)
                cleanup(LogEvent(message=f"오래된 이미지 {len(pruned)}개를 정리했습니다."))
            except Exception:
                pruned = []
                cleanup(
                    LogEvent(
                        level="warn",
                        message="오래된 이미지를 정리하지 못했습니다(배포에는 영향이 없습니다).",
                    )
                )
        except BuildError as exc:
            return fail(exc.error, "build", {"stderr": exc.tail} if exc.tail else None)
        except TerraformError as exc:
            return fail(exc.error, "foundation", {"stderr": exc.tail} if exc.tail else None)
        except DnsError as exc:
            return fail(exc.error, "foundation")
        except (
            Exception
        ) as exc:  # 예기치 않은 오류. 문구에는 원문을 싣지 않고 종류만 남긴다(비밀이 섞일 수 있다)
            return fail(
                AdapterError(
                    code="deploy_pipeline_error",
                    message="배포 중 예기치 않은 오류가 발생했습니다.",
                    hint="서비스 서버의 로그를 확인해 주세요.",
                    retryable=True,
                ),
                current,
                {"exception": type(exc).__name__},
            )

        details = {**result.details, "image": built.image, "pruned": pruned}
        if foundation_created is not None:
            details["foundation_created"] = foundation_created
            details["foundation"] = (
                ensured.fields()
            )  # 서비스가 환경 레코드에 저장할 값(비밀이 아니다)
        if dns_record is not None:
            details["dns"] = dns_record
        return DeployResult(
            ok=True, url=result.url, image_tag=commit_sha, details=redact_json(details, known)
        )

    def _ensure_dns(self, env, log: LogFn) -> dict:
        name = self._dns.name_for(env.env_id)
        log(LogEvent(message=f"DNS 레코드 확인: {name} -> 호스트 {env.host}"))
        changed = self._dns.ensure(env.env_id, env.host)
        log(
            LogEvent(
                message="DNS 레코드를 새 주소로 맞췄습니다."
                if changed
                else "DNS 레코드가 이미 맞게 설정되어 있습니다."
            )
        )
        return {"record": name, "changed": changed}

    def _entry(self, env: Environment, set_name: SetName, stage: str, log: LogFn):
        """배포 말고 진입점(연결 확인, 삭제, 환경 정리)이 함께 쓰는 준비: 세트와 환경 종류가 맞는지 보고, 로그를 큰 단계 하나로 정리한다.
        맞지 않으면 (None, 오류, None)을 돌려준다."""
        adapter = self._adapters.get(set_name)
        if adapter is None or SET_KINDS.get(set_name) != env.kind:
            return (
                None,
                AdapterError(
                    code="set_not_supported",
                    message=f"이 환경에서는 '{set_name}' 세트를 사용할 수 없습니다.",
                ),
                None,
            )
        safe = make_safe_log(log)

        def forward(event: LogEvent) -> None:
            prefix = f"[{event.step}/{event.total}] " if event.step and event.total else ""
            safe(
                LogEvent(
                    level=event.level,
                    step=1,
                    total=1,
                    name=stage,
                    message=(prefix + event.message)[:2000],
                )
            )

        return adapter, None, (safe, forward)

    def check(self, env: Environment, log: LogFn, *, set_name: SetName) -> CheckResult:
        """서버에 닿고 쓸 수 있는지 확인한다(온프레미스는 환경을 등록할 때). 성공하면 `details`에 어댑터가 알려 주는 값(공개 IP 등)이 담긴다.

        배포 안의 "연결 확인"과 같은 어댑터 함수를 쓴다. AWS 세트는 공용 기반이 있어야 통과하므로 등록 확인에는 쓰지 않는다."""
        adapter, refused, channels = self._entry(env, set_name, "연결 확인", log)
        if adapter is None:
            return CheckResult(ok=False, error=refused)
        safe, forward = channels
        try:
            result = adapter.check(env, forward)
        except Exception as exc:  # 원문에는 비밀이 섞일 수 있어 종류만 남긴다
            result = CheckResult(
                ok=False,
                error=AdapterError(
                    code="check_pipeline_error",
                    message="연결 확인 중 예기치 않은 오류가 발생했습니다.",
                    hint="서비스 서버의 로그를 확인해 주세요.",
                    retryable=True,
                ),
                details={"exception": type(exc).__name__},
            )
        if not result.ok:
            safe(
                LogEvent(
                    level="error", step=1, total=1, name="연결 확인", message=result.error.message
                )
            )
        return result

    def status(self, env: Environment, app: str, log: LogFn, *, set_name: SetName) -> StatusResult:
        adapter, refused, channels = self._entry(env, set_name, "상태 확인", log)
        if adapter is None:
            return StatusResult(ok=False, error=refused)
        try:
            channels[1](LogEvent(message="앱 상태를 확인합니다."))
            return redact_model(adapter.status(env, app))
        except Exception:
            return StatusResult(
                ok=False,
                error=AdapterError(
                    code="status_pipeline_error",
                    message="상태를 확인하지 못했습니다.",
                    retryable=True,
                ),
            )

    def rollback(
        self, env: Environment, app: str, image_tag: str, log: LogFn, *, set_name: SetName
    ) -> DeployResult:
        adapter, refused, channels = self._entry(env, set_name, "롤백", log)
        if adapter is None:
            return DeployResult(ok=False, error=refused)
        try:
            return redact_model(adapter.rollback(env, app, image_tag, channels[1]))
        except Exception:
            return DeployResult(
                ok=False,
                error=AdapterError(
                    code="rollback_pipeline_error",
                    message="롤백을 완료하지 못했습니다.",
                    retryable=True,
                ),
            )

    def check_transfer_source(
        self,
        env: Environment,
        app: str,
        log: LogFn,
        *,
        set_name: SetName,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> TransferResult:
        """데이터를 옮기기 전에 원본만 미리 확인한다(아무것도 멈추거나 바꾸지 않는다). 성공하면 `details["bytes"]`가 DB 크기다."""
        adapter, refused, channels = self._entry(env, set_name, "원본 확인", log)
        if adapter is None:
            return TransferResult(ok=False, error=refused)
        if not _APP.match(app):
            return TransferResult(
                ok=False,
                error=AdapterError(
                    code="invalid_spec", message="앱 이름 형식이 올바르지 않습니다."
                ),
            )
        if not hasattr(adapter, "data_endpoint"):
            return TransferResult(
                ok=False,
                error=AdapterError(
                    code="transfer_not_supported",
                    message="이 환경은 데이터 이전을 지원하지 않습니다.",
                ),
            )
        try:
            return redact_model(
                inspect_source(adapter.data_endpoint(env, app), channels[1], max_bytes=max_bytes)
            )
        except TransferInputError as exc:
            return TransferResult(ok=False, error=redact_model(exc.error))
        except Exception:
            return TransferResult(
                ok=False,
                error=AdapterError(
                    code="transfer_pipeline_error",
                    message="원본을 확인하지 못했습니다.",
                    retryable=True,
                ),
            )

    def transfer_data(
        self,
        source_env: Environment,
        target_env: Environment,
        app: str,
        log: LogFn,
        *,
        source_set: SetName,
        target_set: SetName,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> TransferResult:
        """앱의 DB를 한 환경에서 다른 환경으로 옮긴다(예: 온프레미스에서 AWS로). DB만 옮기고 볼륨의 파일과 생성된 비밀은 옮기지 않는다.

        대상 환경에는 같은 앱이 먼저 배포되어 있어야 한다. 성공하면 원본 앱은 멈춘 채로 남는다(확인한 뒤 사용자가 지운다).
        실패하면 멈춘 앱을 모두 되돌린다. 로그는 큰 단계 하나("데이터 이전")로 정리된다."""
        adapter, refused, channels = self._entry(source_env, source_set, "데이터 이전", log)
        if adapter is None:
            return TransferResult(ok=False, error=refused)
        target_adapter = self._adapters.get(target_set)
        if target_adapter is None or SET_KINDS.get(target_set) != target_env.kind:
            return TransferResult(
                ok=False,
                error=AdapterError(
                    code="set_not_supported",
                    message=f"대상 환경에서는 '{target_set}' 세트를 사용할 수 없습니다.",
                ),
            )
        if not _APP.match(app):
            return TransferResult(
                ok=False,
                error=AdapterError(
                    code="invalid_spec", message="앱 이름 형식이 올바르지 않습니다."
                ),
            )
        if source_set == target_set and source_env == target_env:
            return TransferResult(
                ok=False,
                error=AdapterError(
                    code="same_environment",
                    message="원본과 대상이 같은 환경입니다.",
                    hint="다른 환경을 대상으로 선택해 주세요.",
                ),
            )
        if not (hasattr(adapter, "data_endpoint") and hasattr(target_adapter, "data_endpoint")):
            return TransferResult(
                ok=False,
                error=AdapterError(
                    code="transfer_not_supported",
                    message="이 환경 조합은 데이터 이전을 지원하지 않습니다.",
                ),
            )
        try:
            source_endpoint = adapter.data_endpoint(source_env, app)
            target_endpoint = target_adapter.data_endpoint(target_env, app)
            return redact_model(
                transfer_database(
                    source_endpoint, target_endpoint, channels[1], max_bytes=max_bytes
                )
            )
        except TransferInputError as exc:
            return TransferResult(ok=False, error=redact_model(exc.error))
        except Exception:
            return TransferResult(
                ok=False,
                error=AdapterError(
                    code="transfer_pipeline_error",
                    message="데이터를 옮기지 못했습니다.",
                    retryable=True,
                ),
            )

    def destroy(
        self, env: Environment, app: str, log: LogFn, *, set_name: SetName
    ) -> DestroyResult:
        """배포한 앱을 지운다(컨테이너, 볼륨, 앱 디렉터리). 앱 DB와 공용 기반, DNS 레코드는 남긴다.

        세트와 환경 종류가 맞는지 먼저 보고, 어댑터의 결과를 그대로 돌려주되 예기치 않은 예외는 종류만 남긴다(원문에 비밀이 섞일 수 있다)."""
        adapter, refused, channels = self._entry(env, set_name, "앱 삭제", log)
        if adapter is None:
            return DestroyResult(ok=False, error=refused)
        safe, forward = channels
        try:
            result = adapter.destroy(env, app, forward)
        except Exception as exc:
            result = DestroyResult(
                ok=False,
                error=AdapterError(
                    code="destroy_pipeline_error",
                    message="삭제 중 예기치 않은 오류가 발생했습니다.",
                    hint="서비스 서버의 로그를 확인해 주세요.",
                    retryable=True,
                ),
                details={"exception": type(exc).__name__},
            )
        if not result.ok:
            safe(
                LogEvent(
                    level="error", step=1, total=1, name="앱 삭제", message=result.error.message
                )
            )
        return result

    def remove_environment(
        self, env: Environment, log: LogFn, *, set_name: SetName
    ) -> DestroyResult:
        """환경을 서비스에서 지울 때 환경 단위로 만든 것을 정리한다(온프레미스: 환경의 DNS 레코드). 서버에는 접속하지 않는다.

        레코드는 환경의 모든 앱이 함께 쓰므로 **이 환경에 남은 앱이 없을 때만** 불러야 한다(어댑터는 남은 앱을 세지 않는다).
        환경 단위 정리가 없는 세트(AWS)는 오류(`remove_environment_unsupported`)를 돌려준다."""
        adapter, refused, channels = self._entry(env, set_name, "환경 정리", log)
        if adapter is None:
            return DestroyResult(ok=False, error=refused)
        safe, forward = channels
        remover = getattr(adapter, "remove_environment_dns", None)
        if remover is None:
            return DestroyResult(
                ok=False,
                error=AdapterError(
                    code="remove_environment_unsupported",
                    message="이 세트는 환경 단위 정리를 지원하지 않습니다.",
                    hint="환경 단위 자원(공용 기반 등)은 별도로 정리해 주세요.",
                ),
            )
        try:
            result = remover(env, forward)
        except Exception as exc:
            result = DestroyResult(
                ok=False,
                error=AdapterError(
                    code="remove_environment_pipeline_error",
                    message="환경 정리 중 예기치 않은 오류가 발생했습니다.",
                    hint="서비스 서버의 로그를 확인해 주세요.",
                    retryable=True,
                ),
                details={"exception": type(exc).__name__},
            )
        if not result.ok:
            safe(
                LogEvent(
                    level="error", step=1, total=1, name="환경 정리", message=result.error.message
                )
            )
        return result

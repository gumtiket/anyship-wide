"""비공개 하위 프로세스 진입점. 앱 모듈을 불러오지 않고 Docker 게이트도 실행하지 않는다."""

import json
import sys
from pathlib import Path

from .analyses import make_diff
from .analysis_contract import ProposalFile, WorkerResult
from .analysis_source import blob_sha


def analyze(repo, output, request, log):
    from ai import run_analysis
    from ai.detectors import RepoView
    from ai.security import SourceMasker
    from ai.transform.workspace import Workspace, patch_paths, source_files

    mode = request["provider"]
    client = None
    if mode == "fake":
        from ai.llm.fake import FakeLLMClient, recommendation_response

        client = FakeLLMClient(
            {
                "diagnose": ['{"explanations":[],"candidates":[]}'],
                "transform": ['{"diff":"","violation_ids":[]}'] * 30,
                "recommend": [recommendation_response],
            }
        )
    elif mode == "bedrock":
        from ai.llm.bedrock import BedrockClient

        client = BedrockClient(schema_retries=1, transport_attempts=2)
    elif mode == "anthropic":
        from ai.llm.anthropic import AnthropicClient

        client = AnthropicClient(schema_retries=1, transport_attempts=2)

    # 프로세스 실행 시간뿐 아니라 논리적 모델 호출 총수도 제한한다. 클라이언트마다
    # 논리 호출 하나당 스키마 시도 최대 2회, 전송 시도 최대 2회다.
    if client is not None:
        original_complete, calls = client.complete, 0

        def limited(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > request["max_calls"]:
                from ai.llm.base import LLMError

                raise LLMError("모델 호출 상한을 초과했습니다.", transport_attempts=0)
            return original_complete(*args, **kwargs)

        client.complete = limited

    result = run_analysis(
        repo,
        out_dir=output,
        target_env=request["target_env"],
        commit=request["base_sha"],
        source_repo=request["source_repo"],
        app_name=request["app_name"],
        llm=client,
        decision_llm=client,
        no_gate=True,
        log=log,
    )
    try:
        report = result.model_dump(mode="json", exclude={"output_files", "build_context"})
        report["provider"] = mode
        if mode in ("none", "fake"):
            report["cost"]["external_calls"] = 0
        report["adapter_compatible"] = False
        if result.deploy_spec is not None:
            from anyship_adapters.spec import SpecError, parse_spec

            try:
                parse_spec(result.deploy_spec.model_dump(mode="json", exclude_none=True))
                report["adapter_compatible"] = True
            except SpecError:
                report["packaging_warnings"].append(
                    {
                        "code": "adapter_spec_rejected",
                        "message": "배포 어댑터가 생성 명세를 수용하지 않았습니다.",
                    }
                )
        files = []
        if result.status != "failed" and result.transformation.patch_valid:
            view = RepoView(repo)
            original = source_files(view)
            masker = SourceMasker(view)
            diff = (Path(output) / "changes.diff").read_bytes().decode("utf-8")
            allowed = set(original) | set(result.transformation.changed_files)
            paths = patch_paths(diff, allowed) if diff.strip() else set()
            with Workspace(original) as workspace:
                workspace.apply(diff)
                changed = workspace.read(paths)
            if result.deploy_spec is not None:
                for name in ("Dockerfile", "deploy-spec.yaml", ".dockerignore"):
                    changed[name] = (Path(output) / name).read_bytes().decode("utf-8")
            # 삭제된 줄과 문맥 줄도 검토 화면과 PR에 그대로 보인다.
            if masker.contains_sensitive(make_diff(original, changed)):
                raise ValueError("sensitive_diff")
            for name, content in sorted(changed.items()):
                if content == original.get(name):
                    continue
                if masker.contains_sensitive(content):
                    raise ValueError("sensitive_proposal")
                files.append(
                    ProposalFile(
                        path=name,
                        content=content,
                        before_sha=blob_sha(original[name]) if name in original else None,
                    )
                )
        return WorkerResult(base_sha=request["base_sha"], report=report, files=files)
    finally:
        if result.build_context is not None:
            result.build_context.cleanup()


def main():
    repo, output, request_path, result_path = map(Path, sys.argv[1:])
    request = json.loads(request_path.read_text(encoding="utf-8"))

    def log(stage, _message):
        # 엔진·모델의 자유 형식 메시지에는 소스나 로컬 경로가 들어 있을 수 있다.
        print(json.dumps({"stage": stage.value}, ensure_ascii=False), flush=True)

    try:
        result = analyze(repo, output, request, log)
        Path(result_path).write_text(result.model_dump_json(), encoding="utf-8")
        return 0
    except Exception:
        print(json.dumps({"error": "analysis_failed"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

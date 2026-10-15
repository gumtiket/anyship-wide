import argparse
import json
import sys
from pathlib import Path

from ai.gate.runner import DockerCliRunner, FakeRunner
from ai.llm import AnthropicClient, BedrockClient, FakeLLMClient, LLMError
from ai.llm.fake import recommendation_response
from ai.llm.recording import DEFAULT_FIXTURES, PlaybackError, RecordingClient, ReplayClient
from ai.pipeline import run_analysis


def check_llm(args: argparse.Namespace) -> int:
    """짧은 요청 1회. 스키마·전송 재시도를 하지 않고 소스 코드도 보내지 않는다."""
    report = {"provider": args.llm, "tier": args.tier, "status": "failed"}
    llm = None
    try:
        factory = AnthropicClient if args.llm == "anthropic" else BedrockClient
        llm = factory(schema_retries=0, transport_attempts=1)
        result = llm.complete(
            "Reply with exactly one word: OK.",
            "Connection check.",
            tier=args.tier,
            schema=None,
            stage="llm_check",
        )
        report.update(
            status="success",
            requested_model=llm.models[args.tier],
            model_id=result.model_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_s=result.latency_s,
            cost_usd=result.cost_usd,
            stop_reason=result.stop_reason,
            usage=result.usage,
            request_parameters=llm.request_parameters(args.tier),
        )
    except (LLMError, ValueError) as error:
        # 예외 메시지, 요청 본문, SDK 객체, 헤더는 직렬화하지 않는다.
        report.update(
            error_type=type(error).__name__,
            error_code=getattr(error, "code", "InvalidConfiguration"),
            error_details=getattr(error, "details", {}),
            transport_attempts=getattr(error, "transport_attempts", 0),
        )
    if llm is not None:
        report["cost"] = llm.tracker.report().model_dump(mode="json")
    output = Path(args.out) / "llm-check.json"
    try:
        if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
            raise OSError("symlink")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        print("[실패] LLM 확인 결과를 저장할 수 없습니다.", file=sys.stderr)
        return 2
    if report["status"] != "success":
        print(f"[실패] {report['error_code']}; 결과: {output}", file=sys.stderr)
        return 1
    print(
        f"{report['model_id']}: 성공, 입력={report['input_tokens']}, "
        f"출력={report['output_tokens']}, 지연={report['latency_s']:.3f}s, "
        f"비용=${report['cost_usd']}; 결과: {output}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Team Bronze AI CLI: P5 추천·샘플 검증·재시도")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("llm-check", help="짧은 유료 확인 호출 1회 (재시도 없음)")
    check.add_argument("--llm", choices=("anthropic", "bedrock"), default="anthropic")
    check.add_argument("--tier", choices=("strong", "fast"), default="fast")
    check.add_argument("--out", default="out/llm-check")
    analyze = commands.add_parser("analyze", help="로컬 레포 분석")
    analyze.add_argument("repo_path")
    analyze.add_argument("--env", choices=("aws", "onprem"), default="aws")
    analyze.add_argument("--out", default="out")
    analyze.add_argument(
        "--save-llm-trace",
        action="store_true",
        help="out에 마스킹된 프롬프트·응답·파싱·규칙 비교를 추가 저장",
    )
    analyze.add_argument(
        "--llm",
        choices=("none", "fake", "bedrock", "anthropic", "record", "replay"),
        default="none",
        help="none: 규칙/템플릿만, fake: 오프라인, bedrock/anthropic: 유료 실제 호출",
    )
    analyze.add_argument(
        "--artifact-llm",
        choices=("none", "fake", "bedrock", "anthropic"),
        default="none",
        help="Dockerfile/명세 제안에 쓸 LLM. bedrock/anthropic은 추가 유료 호출",
    )
    analyze.add_argument(
        "--llm-provider",
        choices=("bedrock", "anthropic"),
        default="bedrock",
        help="record/replay의 기대 제공자 (기본 bedrock)",
    )
    analyze.add_argument("--source-repo", default=None)
    analyze.add_argument("--app-name", default=None, help="A가 확정한 앱 이름 (3~31자 DNS 라벨)")
    analyze.add_argument(
        "--max-request-seconds", type=int, default=10, help="사용자/A의 요청 시간 후보; 실측값 아님"
    )
    analyze.add_argument("--llm-fixtures", default=str(DEFAULT_FIXTURES))
    analyze.add_argument("--use-demo-cache", action="store_true")
    analyze.add_argument("--demo-cache-dir", default=None)
    analyze.add_argument("--commit", default=None)
    analyze.add_argument("--profile", choices=("dev", "prod"), default="dev")
    analyze.add_argument(
        "--gate",
        choices=("none", "fake", "docker"),
        default="none",
        help="none: 실행 생략, fake: 순서 검사, docker: 자체 샘플 실제 실행",
    )
    analyze.add_argument(
        "--no-gate", action="store_true", help="게이트 실행 생략 (--gate보다 우선)"
    )
    analyze.add_argument("--no-compare", action="store_true", help="원본 복사본 비교 실행 생략")
    analyze.add_argument(
        "--image-tag", default=None, help="A가 확정한 외부 이미지 식별자 (내용에 주입하지 않음)"
    )
    analyze.add_argument(
        "--tfvars-json", default=None, help="임시 변수 입력 JSON 파일; 범위 오류는 기본값 대체"
    )
    args = parser.parse_args(argv)
    if args.command == "llm-check":
        return check_llm(args)
    result = None
    try:
        llm = (
            BedrockClient()
            if args.llm == "bedrock"
            else AnthropicClient()
            if args.llm == "anthropic"
            else FakeLLMClient(
                {
                    "diagnose": ['{"explanations":[],"candidates":[]}'],
                    "recommend": [recommendation_response],
                }
            )
            if args.llm == "fake"
            else None
        )
        if args.llm == "record":
            backend = AnthropicClient() if args.llm_provider == "anthropic" else BedrockClient()
            llm = RecordingClient(backend, args.repo_path, Path(args.llm_fixtures))
        elif args.llm == "replay":
            llm = ReplayClient(args.repo_path, Path(args.llm_fixtures), provider=args.llm_provider)
        if args.llm in {"record", "replay"} and args.artifact_llm != "none":
            raise ValueError(
                "record/replay의 패키징은 고정 템플릿입니다. artifact-llm은 none으로 두세요."
            )
        artifact_llm = (
            llm
            if args.artifact_llm in {"bedrock", "anthropic"} and args.artifact_llm == args.llm
            else BedrockClient()
            if args.artifact_llm == "bedrock"
            else AnthropicClient()
            if args.artifact_llm == "anthropic"
            else FakeLLMClient({"dockerfile": ["{}"], "spec": ["{}"]})
            if args.artifact_llm == "fake"
            else None
        )
        overrides = json.loads(Path(args.tfvars_json).read_text()) if args.tfvars_json else None
        if overrides is not None and not isinstance(overrides, dict):
            raise ValueError("tfvars JSON은 객체여야 합니다.")
        result = run_analysis(
            args.repo_path,
            target_env=args.env,
            out_dir=args.out,
            llm=llm,
            runner=DockerCliRunner()
            if args.gate == "docker"
            else FakeRunner()
            if args.gate == "fake"
            else None,
            artifact_llm=artifact_llm,
            decision_llm=llm,
            repair_llm=llm,
            image_reference=args.image_tag,
            tfvars_overrides=overrides,
            no_gate=args.no_gate,
            compare_original=not args.no_compare,
            source_repo=args.source_repo,
            app_name=args.app_name,
            max_request_seconds=args.max_request_seconds,
            commit=args.commit,
            profile=args.profile,
            save_llm_trace=args.save_llm_trace,
            use_demo_cache=args.use_demo_cache,
            demo_cache_dir=args.demo_cache_dir,
        )
        if isinstance(llm, ReplayClient) and result.execution_source != "demo_cache":
            llm.assert_consumed()
    except (ValueError, OSError, PlaybackError) as error:
        print(f"[실패] {error}", file=sys.stderr)
        return 2
    finally:
        if result is not None and result.build_context is not None:
            result.build_context.cleanup()
    return (
        1
        if result.status in {"failed", "unsupported"}
        or result.diagnosis.enrichment_status == "failed"
        else 0
    )

"""명시한 녹화 고정 데이터를 갱신하거나, 오프라인 골든 투영을 검토·승인한다."""

import argparse
import json
from pathlib import Path
from tempfile import mkdtemp

from ai.demo_cache import DEMO_ENVIRONMENTS
from ai.golden import core_result
from ai.llm import AnthropicClient, BedrockClient
from ai.llm.recording import DEFAULT_FIXTURES, RecordingClient, ReplayClient, write_json
from ai.pipeline import run_analysis


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--record", action="store_true", help="선택한 제공자의 유료 호출로 LLM fixture 재기록"
    )
    parser.add_argument(
        "--accept", action="store_true", help="검토한 새 골든 기준을 명시적으로 저장"
    )
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--llm-provider", choices=("bedrock", "anthropic"), default="bedrock")
    parser.add_argument(
        "--demo-env",
        action="store_true",
        help="todo=onprem, todo-scheduler=aws. 생략하면 기존 AWS 골든 설정",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    (root / "out").mkdir(exist_ok=True)
    output = Path(mkdtemp(prefix="golden-refresh-", dir=root / "out"))
    print(f"검토용 결과: {output}")
    failed = False
    for name in ("todo", "todo-scheduler"):
        repo = root / "samples" / name
        client = (
            RecordingClient(
                AnthropicClient() if args.llm_provider == "anthropic" else BedrockClient(),
                repo,
                args.fixtures,
            )
            if args.record
            else ReplayClient(repo, args.fixtures, provider=args.llm_provider)
        )
        result = run_analysis(
            repo,
            out_dir=output / name,
            source_repo=f"sample://{name}",
            target_env=DEMO_ENVIRONMENTS[name] if args.demo_env else "aws",
            llm=client,
            decision_llm=client,
        )
        try:
            if result.diagnosis.enrichment_status != "completed":
                raise ValueError("golden_requires_successful_analysis")
            if isinstance(client, ReplayClient):
                client.assert_consumed()
            actual = core_result(result)
            path = root / "ai/tests/fixtures/golden" / f"{name}.json"
            if args.accept:
                write_json(path, actual)
                print(f"{name}: 의도한 골든 기준 저장")
            elif not path.exists() or json.loads(path.read_text()) != actual:
                failed = True
                print(f"{name}: 골든 기준 차이. {output}를 검토한 뒤 --accept로 반영하세요.")
            else:
                print(f"{name}: 기존 골든 일치")
        finally:
            if result.build_context is not None:
                result.build_context.cleanup()
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

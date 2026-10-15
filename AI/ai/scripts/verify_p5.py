"""소유한 두 샘플에 대해 명시적으로 실행하는 P5 측정. 배포나 Git 쓰기는 하지 않는다."""

import argparse
import json
from pathlib import Path
from tempfile import mkdtemp

from ai.gate.runner import DockerCliRunner, FakeRunner
from ai.llm import BedrockClient, FakeLLMClient
from ai.llm.fake import recommendation_response
from ai.pipeline import run_analysis


def main() -> int:
    parser = argparse.ArgumentParser(description="P5 샘플별 실행 시간·규칙·게이트 측정")
    parser.add_argument("--llm", choices=("fake", "bedrock"), required=True)
    parser.add_argument("--gate", choices=("fake", "docker"), required=True)
    parser.add_argument("--out", default="out/p5-verification")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = Path(args.out).resolve()
    if output.is_relative_to(root / "samples"):
        raise ValueError("측정 출력은 샘플 밖에 지정하세요.")
    output.mkdir(parents=True, exist_ok=True)
    output = Path(mkdtemp(prefix="measurement-", dir=output))
    rows = []
    for name, expected in (("todo", "aws-serverless"), ("todo-scheduler", "aws-always-on")):
        client = (
            BedrockClient()
            if args.llm == "bedrock"
            else FakeLLMClient(
                {
                    "diagnose": ['{"explanations":[],"candidates":[]}'],
                    "recommend": [recommendation_response],
                }
            )
        )
        runner = DockerCliRunner() if args.gate == "docker" else FakeRunner()
        result = run_analysis(
            root / "samples" / name,
            out_dir=output / name,
            llm=client,
            decision_llm=client,
            repair_llm=client,
            runner=runner,
            save_llm_trace=True,
        )
        try:
            row = {
                "sample": name,
                "status": result.status,
                "selected_set": result.recommendation.set,
                "expected_set": expected,
                "rule_fired": result.recommendation.rule_fired,
                "rationale_source": result.recommendation.rationale_source,
                "diagnosis_enrichment": result.diagnosis.enrichment_status,
                "gate_status": result.gate_report.status,
                "transformed": result.gate_report.transformed.status,
                "original": result.gate_report.original.status,
                "gate_attempts": len(result.gate_report.attempts),
                "cleanup_errors": result.gate_report.transformed.cleanup_errors
                + result.gate_report.original.cleanup_errors,
                "pr_eligible": result.gate_report.pr_eligible,
                "needs_confirmation": result.recommendation.needs_confirmation,
                "needs_approval": result.recommendation.needs_approval,
                "timings_s": result.timings_s,
                "llm_usage": result.cost.total.model_dump(mode="json"),
                "llm_stages": {
                    stage: value.model_dump(mode="json")
                    for stage, value in result.cost.stages.items()
                },
                "output": str(output / name),
            }
            row["verified"] = (
                result.recommendation.set == expected
                and result.diagnosis.enrichment_status == "completed"
                and result.recommendation.rationale_source == "llm"
                and result.gate_report.transformed.status == "passed"
                and not row["cleanup_errors"]
                and result.gate_report.status == ("passed" if args.gate == "docker" else "skipped")
            )
            rows.append(row)
        finally:
            if result.build_context is not None:
                result.build_context.cleanup()
    report = {"llm": args.llm, "gate": args.gate, "samples": rows}
    (output / "measurement.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    table = [
        "# P5 측정 결과",
        "",
        f"LLM={args.llm}, runner={args.gate}. 실제 인프라 적용·배포는 하지 않았습니다.",
        "",
        "| 샘플 | 세트 | 분석(초) | 변환(초) | 게이트(초) | 전체(초) | 게이트 결과 |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        timing = row["timings_s"]
        table.append(
            f"| {row['sample']} | {row['selected_set']} | {timing['analysis']:.3f} | "
            f"{timing['transform']:.3f} | {timing['gate']:.3f} | {timing['total']:.3f} | "
            f"{row['gate_status']} |"
        )
    table.extend(
        [
            "",
            "전체 시간에는 패키징·추천 근거 생성·출력 쓰기도 포함됩니다.",
            "tfvars와 인프라 비용은 C 확인 전의 임시값입니다. "
            "LLM 비용 null은 단가 미확정을 뜻합니다.",
            "검증 범위는 두 자체 샘플의 기동·Postgres CRUD입니다. 보류 기능·승인 필요는 남습니다.",
            "Fake는 실제 모델 판단/코드 생성 품질이나 런타임 통과를 증명하지 않습니다.",
        ]
    )
    (output / "measurement.md").write_text("\n".join(table) + "\n")
    print("\n".join(table))
    return 0 if all(row["verified"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

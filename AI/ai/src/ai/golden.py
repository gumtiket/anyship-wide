"""안정적인 회귀 비교용 투영. 과거 모델 출력은 독립된 검증용 데이터가 아니다."""

import hashlib
from pathlib import Path

from ai.models import AnalysisResult
from ai.transform.dockerfile import lint_dockerfile


def core_result(result: AnalysisResult) -> dict:
    dockerfile = Path(result.output_files["Dockerfile"]).read_text()
    diff = Path(result.output_files["changes.diff"]).read_text()
    spec = result.deploy_spec.model_dump(mode="json", exclude_none=True)
    spec.pop("source", None)  # 외부 출처 정보는 앱 동작이 아니다.
    return {
        "support_grade": result.diagnosis.support_grade,
        "violations": sorted(v.id for v in result.diagnosis.violations if v.source == "rule"),
        "factor_reviews": [r.model_dump(mode="json") for r in result.diagnosis.factor_reviews],
        "signals": [s.model_dump(mode="json") for s in result.diagnosis.signals],
        "set": result.recommendation.set,
        "rule_fired": result.recommendation.rule_fired,
        "spec": spec,
        "tfvars": result.recommendation.tfvars,
        "needs_confirmation": result.recommendation.needs_confirmation,
        "needs_approval": result.recommendation.needs_approval,
        "transformation": result.transformation.model_dump(mode="json"),
        "dockerfile_sha256": hashlib.sha256(dockerfile.encode()).hexdigest(),
        "dockerfile_lint": lint_dockerfile(dockerfile),
        "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
    }

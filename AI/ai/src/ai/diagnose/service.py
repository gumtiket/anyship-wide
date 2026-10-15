import json
from importlib.resources import files

from pydantic import Field

from ai.detectors.repo import RepoView
from ai.llm.base import LLMClient
from ai.models import Diagnosis, FactorReview, OutputModel, ReviewCandidate, WarningItem
from ai.security import SourceMasker

FACTOR_OWNERS = (
    ("코드베이스", "구조"),
    ("의존성", "AI 진단"),
    ("설정", "AI 변환 + 어댑터"),
    ("외부 자원", "AI 변환 + 어댑터"),
    ("빌드·릴리스·실행 분리", "서비스 + 어댑터"),
    ("프로세스(무상태)", "AI 진단"),
    ("포트 바인딩", "AI 변환 + 어댑터"),
    ("동시성", "어댑터"),
    ("폐기 가능성", "어댑터"),
    ("개발·운영 일치", "구조"),
    ("로그", "AI 변환 + 어댑터"),
    ("관리 프로세스", "어댑터"),
)
INSPECTED_FACTORS = frozenset({2, 3, 4, 6, 7, 11})
RULE_EXPLANATIONS = {
    "sqlite_usage": (
        "SQLite 주소를 코드에서 발견했습니다.",
        "파일 DB의 저장 수명과 다중 인스턴스 사용을 검토해야 합니다.",
    ),
    "hardcoded_secret": (
        "인증 설정 이름의 변수에 리터럴이 있습니다.",
        "환경별 설정 분리와 시크릿 교체가 필요합니다.",
    ),
    "file_log": (
        "로그를 파일로 쓰는 설정입니다.",
        "읽기 전용 실행 조건에서 파일 생성이 실패할 수 있습니다.",
    ),
    "fixed_port": (
        "Uvicorn 포트가 리터럴입니다.",
        "환경별 PORT 값을 주입하려면 시작 설정을 변경해야 합니다.",
    ),
    "local_file_write": (
        "파일 쓰기 호출 후보입니다.",
        "영속 저장인지 확인하고 저장 방식 변경 전 승인이 필요합니다.",
    ),
    "unpinned_dependency": (
        "일부 의존성의 정확한 버전 고정이 미확인입니다.",
        "재설치 시 다른 버전이 선택될 수 있습니다.",
    ),
    "hardcoded_db_url": (
        "DB 주소가 코드/설정 리터럴입니다.",
        "환경별 연결 주소를 설정으로 분리해야 합니다.",
    ),
}


class Explanation(OutputModel):
    id: str
    description: str
    impact: str
    factor: int


class Candidate(OutputModel):
    factor: int
    file: str
    line: int = Field(ge=1)
    evidence: str
    description: str


class Enrichment(OutputModel):
    explanations: list[Explanation] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)


def review_factors(diagnosis: Diagnosis) -> list[FactorReview]:
    hits = {v.factor for v in diagnosis.violations if v.source == "rule"}
    if any(v.rule == "sqlite_usage" for v in diagnosis.violations if v.source == "rule"):
        hits.add(4)  # 로컬 SQLite URL도 외부에서 붙인 DB 주소가 없는 경우에 해당한다.
    incomplete = any(w.code in {"scan_limit", "python_syntax_error"} for w in diagnosis.warnings)
    result = []
    for number, (principle, owner) in enumerate(FACTOR_OWNERS, 1):
        if number not in INSPECTED_FACTORS or diagnosis.support_grade == "unsupported":
            status = "n/a"
        elif number in hits:
            status = "violation"
        else:
            status = "n/a" if incomplete else "ok"
        result.append(FactorReview(factor=number, principle=principle, owner=owner, status=status))
    return result


def enrich(
    diagnosis: Diagnosis, repo: RepoView, llm: LLMClient | None, masker: SourceMasker
) -> Diagnosis:
    result = diagnosis.model_copy(deep=True)
    for violation in result.violations:
        violation.description, violation.impact = RULE_EXPLANATIONS.get(
            violation.rule, ("추가 검토 대상입니다.", "영향을 확인해야 합니다.")
        )
        if violation.rule == "sqlite_usage" and "직접 연결" in violation.evidence:
            violation.description = "SQLite 직접 연결 호출을 발견했습니다."
        if violation.rule == "hardcoded_secret" and not violation.evidence.startswith(
            "인증 정보 이름의 변수"
        ):
            violation.description = "설정·함수 인자·URL에서 자격 증명 리터럴을 발견했습니다."
    result.factor_reviews = review_factors(result)
    if not any(w.code == "rule_assessment_scope" for w in result.warnings):
        result.warnings.append(
            WarningItem(
                code="rule_assessment_scope",
                message="ok는 현재 규칙에서 위반을 찾지 못했다는 뜻입니다. "
                "전체 준수나 실행 안전성을 보장하지 않습니다.",
            )
        )
    if llm is None or result.support_grade == "unsupported":
        return result
    payload = {
        "violations": [v.model_dump(mode="json") for v in result.violations],
        "signals": [s.model_dump(mode="json") for s in result.signals],
        "source": masker.summaries(repo),
        "inspected_factors": sorted(INSPECTED_FACTORS),
    }
    prompt = files("ai").joinpath("prompts/diagnose.txt").read_text(encoding="utf-8")
    try:
        response = llm.complete(
            prompt,
            json.dumps(payload, ensure_ascii=False),
            tier="fast",
            schema=Enrichment,
            stage="diagnose",
        )
        data = response.parsed
        if not isinstance(data, Enrichment):
            raise ValueError("invalid_enrichment_type")
        explanations = {item.id: item for item in data.explanations}
        for violation in result.violations:
            item = explanations.get(violation.id)
            if item:
                if item.description.strip():
                    violation.description = masker.text(item.description)
                if item.impact.strip():
                    violation.impact = masker.text(item.impact)
                if item.factor != violation.factor:
                    result.warnings.append(
                        WarningItem(
                            code="llm_factor_mismatch",
                            message="LLM의 번호 제안은 적용하지 않고 규칙 매핑을 유지했습니다.",
                        )
                    )
        existing = {(v.file, v.line, v.factor) for v in result.violations}
        for candidate in data.candidates:
            if (
                candidate.factor not in INSPECTED_FACTORS
                or candidate.file not in repo.files()
                or candidate.file in masker.blocked_files
                or candidate.line > len(repo.read(candidate.file).splitlines())
                or not candidate.evidence.strip()
                or (candidate.file, candidate.line, candidate.factor) in existing
            ):
                result.warnings.append(
                    WarningItem(
                        code="llm_candidate_rejected",
                        message="검사 범위/근거 위치가 유효하지 않은 LLM 후보를 제외했습니다.",
                    )
                )
                continue
            evidence = masker.text(candidate.evidence).strip()
            source_line = masker.text(repo.read(candidate.file).splitlines()[candidate.line - 1])
            if not evidence or evidence not in source_line:
                result.warnings.append(
                    WarningItem(
                        code="llm_candidate_evidence_mismatch",
                        message="근거와 실제 소스 줄이 일치하지 않는 AI 검토 후보를 제외했습니다.",
                    )
                )
                continue
            result.review_candidates.append(
                ReviewCandidate(
                    id=f"llm_candidate:{candidate.file}:{candidate.line}:{candidate.factor}",
                    factor=candidate.factor,
                    file=candidate.file,
                    line=candidate.line,
                    evidence=evidence,
                    description=masker.text(candidate.description),
                )
            )
            existing.add((candidate.file, candidate.line, candidate.factor))
        result.enrichment_status = "completed"
    except (ValueError, RuntimeError):
        result.enrichment_status = "failed"
        result.warnings.append(
            WarningItem(
                code="llm_enrichment_failed",
                message="LLM 보강 실패로 규칙 결과를 유지했습니다. 원문 오류/응답은 비공개입니다.",
            )
        )
    return result

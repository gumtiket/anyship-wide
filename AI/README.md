# Bronze AI 모듈

FastAPI 저장소를 정적으로 진단하고 코드 변경안, Dockerfile, 배포 명세와 환경 추천을 생성한다. 현재 CLI와 Python 함수만 제공하며 서비스(A)의 웹 AI 제공자 연결은 별도 작업이다.

## 구조

```text
AI/
  ai/          # pyproject.toml, src/ai, tests, scripts, deliverables
  samples/     # todo, todo-scheduler, memo-app 미지원 테스트 픽스처
  docs/        # 작업 분해 원문, 게이트 보안, C 확인 체크리스트
  reference/   # 배포 명세 예시
```

`AI` 디렉터리 안에서 기존의 `ai/`와 `samples/` 상대 경로를 유지한다. 팀 저장소 밖의 고정 절대 경로에 의존하지 않는다.

## 설치·실행

팀 저장소 루트에서 Python 3.12로 실행한다.

```sh
cd AI
python3.12 -m venv ai/.venv
ai/.venv/bin/python -m pip install -e 'ai[dev]' -c ai/requirements-dev.lock
ai/.venv/bin/python -m ai analyze samples/todo --llm fake --gate fake --out out/todo/
ai/.venv/bin/python -m pytest ai/tests -q
ai/.venv/bin/ruff check ai
ai/.venv/bin/ruff format --check ai
```

Fake는 오프라인 흐름 검사용이며 게이트는 `skipped`다. 실제 통과로 표시하지 않는다. 기본 `--llm none`, `--gate none`은 외부 서비스를 호출하지 않는다. CLI 종료 코드는 보고서 생성 성공 0, 미지원/변경안/진단 보강/게이트 실패 1, 입력·출력 오류 2다. 코드 변경안에 보류 항목이 있으면 0이라도 배포 준비 완료가 아니다.

세부 실행·출력 계약은 [패키지 README](ai/README.md), 함수 연결 예시는 [서비스 통합 경계](ai/deliverables/integration.md)에서 확인한다.

## 현재 범위

- 규칙으로 지원 등급, 12-factor II·III·IV·VI·VII·XI 위반, 스케줄러·WebSocket 등의 신호를 판정한다. 나머지 원칙은 담당 주체와 `n/a`만 기록한다.
- LLM은 설명·근거와 제한된 코드 제안을 생성한다. 세트 판정·담당·규칙 판정을 바꿀 수 없다. JSON 스키마 검증은 설명의 사실 정확성을 증명하지 않는다.
- 환경변수·포트·로그 변경안을 생성한다. DB·영속 파일 변경은 `risky`이며 승인이 필요하다. 데이터 이전은 미지원이다.
- 코드/DB 원본은 수정하지 않는다. 모든 적용·기동은 임시 복사본에서만 수행한다.
- 두 자체 샘플만 Docker/임시 Postgres 기동·초기화·CRUD·앱 재기동 후 재조회와 원본 복사본 비교를 지원한다. 일반 앱은 검증 `skipped`, 현재 `pr_eligible=false`다.
- 게이트 실패 시 최초 포함 최대 3회 실행한다. 반복 수정안, 보안/API/환경변수 계약 변경, 비코드 실패는 일찍 중단한다.
- 서버리스/상시 컨테이너 변수 모델과 비용 테이블은 C 확정 전의 임시값이다. `needs_confirmation=["tfvars_schema", "cost_table"]`을 표시하고 [확인 체크리스트](docs/p5-infra-confirmation-checklist.md)를 따른다.

P6까지 구현했다. 두 고정 샘플의 엄격 LLM replay·골든 회귀·사전 실행 캐시를 제공한다. 전체 앱 배포·PR 생성·merge·ECR·어댑터 실행은 이 모듈에서 수행하지 않는다.

## 팀 서비스 연결 상태

웹 서비스는 `service/app/ai_provider.py`가 `service/app/analysis_worker.py`를 별도 프로세스로 실행해 `run_analysis`를 호출하는 방식으로 연결되어 있다. 입력은 클론된 **로컬 레포 경로**이고 출력은 파일 7종이다. 설정과 동작은 [AI 통합 안내](../docs/AI_INTEGRATION.md)를 따른다.

## P6 오프라인 재현

```sh
ai/.venv/bin/python -m ai analyze samples/todo --llm replay --gate fake --out out/replay/
ai/.venv/bin/python -m ai analyze samples/todo --use-demo-cache --out out/demo/
```

캐시는 모든 로그에 (사전 실행 결과)를 표시한다. 현재 게이트는 skipped, 과거 검증은 historical_status=passed이며 현재 API 호출은 0이다. 캐시 비용은 과거 사용량이다. [서비스 연결 문서](docs/integration-for-service.md)와 [인수인계 체크리스트](docs/handoff-checklist.md)를 확인한다. 모델 선택은 [Claude 전달 프롬프트](ai/deliverables/model-selection-prompt.md)로 별도 판단한다.

# AnyShip AI 웹 통합

이슈 [#61](https://github.com/gumtiket/anyship/issues/61)의 첫 단계인 **소스 분석 → 전체 변경안 검토 → Draft PR**을 구현합니다. `AI/ai`의 `run_analysis`는 진단과 변경안을 함께 생성하므로 웹에서도 변경안을 한 묶음으로 검토합니다. 항목별 수정 재실행과 PR 커밋의 배포 연결은 후속 범위입니다.

## 실행 설정

Python 3.12 이상과 Git이 필요합니다. `service/`에서 다음을 실행합니다.

```sh
python -m pip install -r requirements-dev.txt
python -m alembic upgrade head
```

운영 의존성만 설치한 환경에는 `python -m pip install -r requirements-ai.txt`로 AI 패키지를 추가합니다. `frontend/`에서 `pnpm build` 후 서버를 재시작합니다. 기존 실제 GitHub 로그인 설정에 아래 값을 추가합니다. 기존 `.env.github.local`의 모드나 자격 증명을 자동 변경하지 않습니다.

```dotenv
APP_AI_MODE=bronze
APP_AI_PROVIDER=none
APP_AI_TIMEOUT=300
APP_AI_MAX_CALLS=12
```

| 설정 | 동작 |
| --- | --- |
| `APP_AI_MODE=unavailable` | 분석 비활성화. 자동 fallback 없음 |
| `APP_AI_MODE=placeholder` | 기존 개발용 고정 안내 파일·단일 PR 경로 |
| `APP_AI_MODE=bronze` | 실제 AI 패키지·비동기 분석·다중 파일 검토 |
| `APP_AI_PROVIDER=none` | 모델 호출 없이 규칙 진단·지원 템플릿 변환 |
| `APP_AI_PROVIDER=fake` | 실제 파이프라인에 모의 모델 주입. 외부 모델 호출 0회. 운영 금지 |
| `APP_AI_PROVIDER=bedrock` | Bedrock 모델 사용 |
| `APP_AI_PROVIDER=anthropic` | Anthropic 모델 사용. `pip install -e '../AI/ai[anthropic]'` 추가 |

`/api/config`는 모드와 제공자를 표시하며 `ai_available`은 bronze 경로 활성 여부입니다. 모델 인증 성공을 보장하지 않습니다. bronze 모드는 실제 GitHub 인증이 필요하며 데모 로그인과 함께 사용할 수 없습니다.

Bedrock은 `BEDROCK_REGION`, `BEDROCK_MODEL_ID_STRONG`, `BEDROCK_MODEL_ID_FAST`와 **AI 전용** `APP_AI_AWS_ACCESS_KEY_ID`, `APP_AI_AWS_SECRET_ACCESS_KEY`, 필요 시 `APP_AI_AWS_SESSION_TOKEN`을 설정합니다. 서버 인스턴스 역할을 사용할 때만 `APP_AI_USE_INSTANCE_ROLE=true`를 설정합니다. 기본적으로 서버의 일반 AWS 키·프로필·클라우드 배포 자격 증명을 상속하지 않습니다. Anthropic은 `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL_ID_STRONG`, `ANTHROPIC_MODEL_ID_FAST`를 사용합니다. 자격 증명은 서버 설정에만 두며 요청·분석 이력에 저장하지 않습니다.

## 실행과 보존

1. 현재 사용자와 GitHub App의 저장소 접근·쓰기 권한을 확인하고 브랜치의 40자리 SHA를 고정합니다.
2. Git tree/blob API로 정확히 그 커밋의 파일을 내려받아 해시를 확인합니다. 저장소의 코드나 Git hook을 실행하지 않습니다.
3. 별도 Python 프로세스에서 `run_analysis(..., no_gate=True)`를 호출합니다. GitHub 토큰·서비스 DB 연결·토큰 암호화 키를 자식 환경에 전달하지 않습니다. 이 프로세스 분리는 OS 보안 샌드박스는 아닙니다.
4. patch 적용·Python 문법, 파일 경로·원본 blob SHA·크기·배포 명세를 확인합니다. 기존 실행 파일 모드는 유지합니다. 변경 없는 파일은 제외하고 새 Dockerfile·명세·dockerignore도 검토 diff에 포함합니다.
5. 결과·진행 단계·파일 내용·diff·검토 해시를 DB에 저장하고 임시 소스를 정리합니다. 종료·시간 초과 시 자식 프로세스 트리를 종료합니다. 강제 서버 종료로 남은 작업은 재시작 시 `interrupted`로 표시하며 자동 재호출하지 않습니다.
6. 사용자가 전체 diff를 검토하고, 필요한 경우 DB·저장 방식 변경을 별도로 승인한 뒤 Draft PR을 요청합니다. 내용 해시·대상 저장소·기준 SHA를 다시 확인하며 Git 작업의 tree/commit/ref/PR 체크포인트로 재시도합니다. 기존 브랜치를 강제 갱신하거나 자동 병합하지 않습니다.

마이그레이션 `0009`는 `analysis_runs`, `analysis_slots`를 추가하며 기존 placeholder 이력을 유지합니다. 작업 상태는 `queued → running → completed/failed/interrupted`, 게시 상태는 `proposed → publishing → pr_created`입니다. 작업의 `completed`는 엔진 처리 완료이며 분석 결과의 `partial`, `unsupported`, `failed`와 구분합니다. 프로젝트 연결을 삭제하면 분석 이력도 삭제합니다. 처리 중이거나 PR 게시 중이면 삭제를 거부합니다.

서버당 두 작업을 실행하고 대기 포함 네 작업까지 받습니다. 프로젝트별 한 분석만 허용하며 같은 DB·작업 폴더에 대한 프로세스 잠금을 사용합니다. 여러 Uvicorn worker나 여러 호스트에 분산한 실행은 지원하지 않습니다. `APP_AI_WORKSPACE`는 기본 `service/workspaces/analysis`이며 서버의 비공개 로컬 디스크에 둡니다. 강제 프로세스 종료로 남은 `job-*` 임시 폴더는 모든 서버를 종료한 후 관리자가 정리할 수 있습니다.

## 제한과 결과 해석

- 소스 준비 제한 120초, tree 항목 2,000개, 파일 512 KiB, 총 소스 20 MiB. API 호출 한 번의 제한 때문에 준비 시간은 약간 초과할 수 있습니다. `.env*`, 인증 디렉터리, DB·키·로그·일부 생성물은 제외합니다. 링크·서브모듈은 지원하지 않으며 비 UTF-8 파일은 분석에서 제외합니다.
- 변경안은 최대 100파일·2 MiB, 삭제/이름 변경은 지원하지 않습니다. 인식된 비밀 값이 최종 변경 내용이나 diff에 남으면 게시용 결과 생성을 거부합니다. 엔진의 마스킹은 일반적인 비밀 탐지기를 대체하지 않습니다.
- `APP_AI_TIMEOUT`은 자식 프로세스 실행 제한(10~900초), `APP_AI_MAX_CALLS`는 논리 모델 호출 상한(1~30)입니다. 한 논리 호출에 schema/transport 재시도가 있을 수 있으며 정확한 금액 상한은 아닙니다.
- 현재 단일 전역 FastAPI 진입점이 중심 지원 범위입니다. `violations`는 규칙 진단, `review_candidates`는 모델의 추가 검토 후보이며 자동 수정 선택지가 아닙니다. 반영·보류 ID, 위험 변경 승인, 보강 실패를 그대로 표시합니다.
- 웹 워커는 Docker gate를 실행하지 않습니다. gate `skipped`, `pr_eligible=false`를 통과로 바꾸지 않습니다. Draft PR 생성은 전체 기능 검증이나 배포 승인이 아닙니다. 기존 데이터 자동 이전도 지원하지 않습니다.
- 비용 미확인 값은 `null`, 과거 결과는 과거 표시를 유지합니다. 모의 모델 호출과 외부 호출도 구분합니다.
- 생성 명세는 `anyship_adapters.spec.parse_spec`로 확인하지만 실제 배포 호환·성공은 별도 검증이 필요합니다. 현재 웹의 실제 배포는 `aws-always-on`만 지원합니다. `aws-serverless`/`onprem` 추천을 즉시 배포 버튼으로 연결하지 않습니다. 배포는 PR 검토·병합 후 기존 배포 화면에서 별도로 진행합니다.

## API

모든 API는 현재 워크스페이스 소유권을 검사합니다. POST는 로그인 쿠키·Origin·CSRF가 필요하며 현재 GitHub 권한도 재확인합니다.

| API | 본문 / 의미 |
| --- | --- |
| `POST /api/projects/{id}/analyses` | `{request_id: UUID, target_env: "aws" 또는 "onprem"}`. 새 작업 `202`, 동일 요청 재조회 `200` |
| `GET /api/projects/{id}/analyses?limit=30&offset=0` | 이력 요약, 최신순 |
| `GET /api/projects/{id}/analyses/{run_id}` | 단계 로그, 엔진 결과, 파일·diff·검토 해시·게시 상태 |
| `POST /api/projects/{id}/analyses/{run_id}/pr` | `{review_hash, approve_risky: boolean}`. 검토한 묶음을 Draft PR로 제출 |

응답이 유실되면 같은 요청 ID 또는 같은 분석의 PR 요청을 재시도합니다. 실패한 분석을 다시 실행할 때는 새 요청 ID를 사용합니다. 기준 브랜치가 바뀌면 새 분석·재검토가 필요합니다. 기존 `/analysis`, `/changes` 경로는 placeholder 전용으로 유지합니다.

## 검증

`service/`에서 `python -m pytest tests/test_ai_integration.py tests/test_ai_migration.py -q`를 실행합니다. HTTP GitHub fixture, 실제 AI 자식 프로세스와 모의 모델을 사용하며 외부 저장소·AWS를 변경하지 않습니다. 세 샘플의 지원/미지원 결과, 환경 격리·취소·경로 거부, 다중 파일 PR·중복 요청·응답 유실·권한·SHA·검토 무결성·재시작·DB 변경을 확인합니다.

브라우저 검증은 프론트엔드 빌드 후 `python -m tests.browser_fixture --ai`로 별도 임시 DB와 모의 GitHub를 실행하고 `http://127.0.0.1:8001/_test_only/login`을 엽니다. 실제 AI 파이프라인과 모의 모델로 분석·검토·PR 화면을 확인할 수 있습니다.

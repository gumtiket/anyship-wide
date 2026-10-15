# anyship-adapters

서비스가 "이 환경에 이 앱을 배포해 줘"라고 요청할 때 부르는 **어댑터 인터페이스**와,
서비스를 먼저 개발할 수 있게 해 주는 **mock 어댑터**입니다.

환경(AWS, 온프레미스)마다 작동 방식이 다르지만, 서비스는 아래 다섯 개 함수만 부릅니다.

```
서비스 ──(Adapter 인터페이스)──▶ 어댑터 ──▶ 환경
```

- 지금 들어 있는 것: 인터페이스, 데이터 구조, 비밀 걸러내기, **mock 어댑터**
- 아직 없는 것: 실제 어댑터(온프레미스, AWS). 같은 인터페이스를 구현해서 따로 추가합니다.

## 설치

서비스(`service/`)에서 설치합니다. 의존성은 `pydantic` 하나이고, 서비스가 이미 고정한 버전
(`pydantic==2.13.5`)과 호환됩니다.

```bash
python -m pip install -e ../infra/adapters
```

사용할 때는 항상 `anyship_adapters`에서 임포트하면 됩니다.

```python
from anyship_adapters import MockAdapter, OnpremEnvironment, AwsEnvironment
```

## 사용 예

```python
from anyship_adapters import MockAdapter, OnpremEnvironment

adapter = MockAdapter()
env = OnpremEnvironment(env_id="demo", host="3.38.88.141")


def log(event):
    print(f"[{event.step}/{event.total}] {event.name}: {event.message}")


check = adapter.check(env, log)
if not check.ok:
    print(check.error.message, check.error.hint)

spec = {"app": "todo-a1b2", "backing_services": []}
result = adapter.deploy(env, spec, "3f2a9c1", {"API_KEY": "..."}, log, set_name="onprem")
if result.ok:
    print(result.url)  # https://todo-a1b2.demo.onprem.anyship.cloud
else:
    print(result.error.code, result.error.message)
```

실행하면 이렇게 출력됩니다.

```
[1/2] 접속 확인: 환경에 접속하는 중
[2/2] 준비 상태 확인: 필요한 구성 요소를 확인하는 중
[1/5] 환경 점검: 환경 점검 중
...
[5/5] 헬스체크: 헬스체크 중
```

## 인터페이스

| 함수 | 하는 일 | 돌려주는 것 |
|---|---|---|
| `check(env, log)` | 이 환경에 접속하고 관리할 수 있는지 확인 | `CheckResult` |
| `deploy(env, spec, image_tag, secrets, log, *, set_name)` | 선택한 세트로 앱을 배포 | `DeployResult` (`url`, `image_tag`) |
| `status(env, app)` | 앱이 지금 실행 중이고 정상인지 | `StatusResult` (`state`, `url`, `image_tag`) |
| `rollback(env, app, image_tag, log)` | 이전 이미지 태그로 다시 실행(DB 스키마는 되돌리지 않음) | `DeployResult` |
| `destroy(env, app, log)` | 배포가 만든 것을 제거(없어도 성공) | `DestroyResult` |

공통 규칙

- **모든 함수는 작업이 끝날 때까지 기다립니다.** 서비스가 백그라운드 작업자에서 실행하고, 그동안 `log`로
  받은 이벤트를 사용자 화면에 흘려보냅니다.
- **실패는 예외가 아니라 `ok=False` 결과로 돌려줍니다.** 서비스는 `result.ok`만 확인하면 됩니다.
  (코드의 예상하지 못한 버그는 그대로 예외가 됩니다.)
- `deploy`의 `set_name`은 키워드 인자입니다. AWS 환경만으로는 `aws-serverless`와
  `aws-always-on` 중 무엇인지 알 수 없어서, 추가했습니다.

## 데이터 구조

### 환경 (`env`)

사용자가 환경 등록 화면에서 입력한 값입니다. **서비스 DB에 저장해도 되는 값만** 담습니다.
모르는 필드는 거부하므로 개인 키나 토큰은 넣을 수 없습니다.

| 종류 | 필드 | 형식 |
|---|---|---|
| `AwsEnvironment` | `env_id` | 소문자로 시작, 소문자·숫자·하이픈, 2~21자 |
| | `role_arn` | `arn:aws:iam::<12자리>:role/...` (역할만 허용) |
| | `external_id` | 16~128자. **서비스(백엔드)가 환경마다 만들어 저장** |
| | `region` | 기본 `ap-northeast-2` |
| | `host` | (`aws-always-on`) 앱 호스트의 탄력적 IP. 공용 기반의 출력 `host_public_ip` |
| | `ssh_user`, `ssh_port` | 기본 `deploy`, 22 |
| | `db_address` | (`aws-always-on`) 공용 RDS 주소. `*.rds.amazonaws.com`만 허용. 출력 `db_address` |
| | `db_port` | 기본 5432 |
| | `db_secret_arn` | (`aws-always-on`) RDS 마스터 비밀의 ARN. 출력 `db_master_secret_arn`. 비밀번호 자체가 아니다. 계정이 `role_arn`의 계정과 같아야 한다 |
| | `state_bucket` | Terraform state 버킷. 온보딩 스택 출력 `StateBucketName`(이름은 계산할 수 없어 서비스가 저장해 둔다. `AwsAccess.read_state_bucket(env, stack_name)`이 스택 출력에서 읽어 주며, 스택이 없거나 `CREATE_COMPLETE` 등 완료 상태가 아니거나 출력이 우리가 짓는 형식이 아니면 `stack_not_found`, `stack_not_ready`, `stack_output_invalid`로 실패한다). `anyship-tfstate-<계정>-<리전>-<8자>` 형식만 허용하고 계정이 `role_arn`의 계정과 같아야 한다. `terraform_runner`가 쓴다 |
| `OnpremEnvironment` | `env_id` | 위와 같음 |
| | `host` | IP 또는 호스트 이름(SSH 옵션을 끼워 넣을 수 있는 값은 거부) |
| | `ssh_user` | 기본 `deploy` |
| | `ssh_port` | 기본 22 |

두 종류를 한꺼번에 받을 때는 `kind`(`"aws"` 또는 `"onprem"`)로 구분합니다.

```python
from pydantic import TypeAdapter
from anyship_adapters import Environment

env = TypeAdapter(Environment).validate_python(row_from_db)
```

### 명세 (`spec`)와 비밀 (`secrets`)

- `spec`은 배포 명세를 **딕셔너리**로 넘깁니다. 어댑터는 필드를 다시 검증합니다.
- `secrets`는 `{"이름": "값"}` 딕셔너리입니다. **메모리에만 두고 서비스 DB, 로그에 남기지 마세요.**
  실제 어댑터(온프레미스, AWS)는 이 값을 서버의 비밀 파일(`app.env`, 소유자만 읽는 권한 600)에 쓰고, 앱 컨테이너의
  환경변수로 주입합니다. 결과에는 **이름만** 남깁니다: 새로 생성한 비밀의 이름은 `details["generated"]`
  (mock은 받은 비밀의 이름을 `details["secrets_stored"]`로 돌려줍니다).
- 명세의 `generate: true` 비밀(예: `SECRET_KEY`)은 첫 배포에 어댑터가 만들고, 재배포 때는 서버의 값을 재사용합니다.
  **사용자가 입력하는 비밀(`generate`가 아닌 것)은 이전 값을 재사용하지 않으므로, 재배포 때마다 `secrets`로 다시 넘겨야 합니다.**
  빠지면 `missing_secret`으로 실패합니다.

### 로그 이벤트 (`LogEvent`)

| 필드 | 설명 |
|---|---|
| `ts` | 시각(UTC) |
| `level` | `info`, `warn`, `error` |
| `step`, `total` | "5단계 중 2번째" 같은 진행 표시. 퍼센트는 제공하지 않습니다 |
| `name` | 단계 이름(예: `이미지 전달`) |
| `message` | 화면에 보여 줄 문장 |
| `data` | 나중에 이력이나 분석에 쓸 부가 정보(비밀은 들어가지 않음) |

### 결과

모든 결과는 **성공이거나, 오류를 담은 실패 중 하나**입니다(둘 다이거나 둘 다 아닌 경우는 만들 수 없습니다).

| 필드 | 설명 |
|---|---|
| `ok` | 성공 여부 |
| `error.code` | 기계가 읽는 코드(소문자와 밑줄). 화면 분기에 사용 |
| `error.message` | 사용자에게 보여 줄 메시지 |
| `error.hint` | 사용자가 할 수 있는 조치 |
| `error.retryable` | 다시 시도하면 될 수 있는 오류인지 |
| `details` | 부가 정보(예: `check`의 `account_id`, `public_ip`) |

## 세트 이름

| 세트 | 쓸 수 있는 환경 |
|---|---|
| `aws-serverless` | `AwsEnvironment` (Lambda) |
| `aws-always-on` | `AwsEnvironment` (EC2 호스트) |
| `onprem` | `OnpremEnvironment` |

환경과 맞지 않는 세트를 넘기면 `set_not_supported`로 거부합니다.

### `aws-always-on`의 특징

온프레미스와 같은 부품(`ComposeHost`, `render_stack`, `SshRunner`, `redact`)을 쓰고, 다음이 다릅니다.

| | 온프레미스 | `aws-always-on` |
|---|---|---|
| 앱 DB | 앱마다 Postgres **컨테이너**(앱 전용 볼륨) | 사용자 계정의 **공용 RDS** 안에 앱 전용 DB·계정 |
| `DATABASE_URL` | `compose.yaml`의 치환(`.env`의 비밀번호) | `app.env`에만(권한 600). `sslmode=require` |
| 앱 네트워크 | `internal`(인터넷 차단) + `traefik` | `traefik`만. 앱이 VPC의 RDS에 닿아야 하므로 앱이 외부로 나갈 수 있다 |
| 앱 `destroy` | 컨테이너, 볼륨(DB 데이터), 파일 삭제 | 컨테이너와 파일만 삭제. **앱 DB는 남긴다**(데이터 보호, 삭제는 후속) |
| 호스트 정보 | 환경 등록 때 사용자가 입력 | 공용 기반(`infra/user-account`)의 출력을 서비스가 환경 정보에 담아 넘긴다 |
| DNS | 선택적 자동화(`dns=`) | 자동화 없음. `*.<환경ID>.aws.<도메인>` 레코드를 미리 만들어 둔 환경 전제 |

- **공용 기반이 없으면** `host`, `db_address`, `db_secret_arn`이 비어 있고, `check`와 `deploy`는 `foundation_missing`으로 실패합니다.
  기반은 서비스가 `infra/user-account`로 먼저 만듭니다(실행 방법은 그 폴더의 README). 첫 배포 때 약 20분 걸립니다.
- **앱의 비밀 보관**: MVP는 온프레미스와 같이 호스트의 `app.env`(권한 600)에 둡니다. MVP 문서는 "사용자 계정
  Secrets Manager가 원본"이라고 했지만, 이번 범위에서는 구현하지 않기로 했습니다(후속 과제). 그래서 호스트를 교체하면
  생성한 비밀(`SECRET_KEY`)이 새로 만들어져 로그인 세션이 끊기고, 사용자가 입력한 비밀은 다시 넣어야 합니다.
  Secrets Manager에 있는 것은 RDS 마스터 비밀번호뿐입니다.
- **마스터 비밀번호**는 서비스 서버가 사용자 역할로 Secrets Manager에서 읽어 SSH 표준입력으로만 호스트에 전달합니다.
  호스트에는 AWS 자격 증명이 없고, 비밀번호는 명령줄, 로그, 결과, 디스크에 남지 않습니다.
- **`check`가 서비스의 `STSAdapter.check`와 다른 이유**: 서비스의 것은 환경 등록 때 한 번 하는 신뢰 정책 검증
  (External ID 필수 여부, 계정 일치)이고, 이쪽은 배포 직전 점검(역할, 호스트, Docker, Traefik)입니다.
  오류 코드 이름(`access_denied`, `account_mismatch`, `service_credentials_unavailable`, `aws_unavailable`)은 서로 맞췄습니다.
- 이미지는 온프레미스와 같이 서비스 서버의 로컬 이미지를 SSH로 보냅니다(레지스트리 없음). 이미지는 아래 `이미지 빌더`가 만듭니다.
- **마이그레이션용 임시 컨테이너는 Traefik에서 숨깁니다**(`docker compose run --rm --label traefik.enable=false`, 온프레미스도 같음).
  숨기지 않으면 임시 컨테이너가 앱의 Traefik 라벨을 물려받아 같은 서비스의 두 번째 서버로 등록되고, 사라진 뒤 설정이 갱신되기 전까지
  배포 직후 첫 요청에 502가 한 번 납니다(실제 호스트에서 재현했고 고친 뒤 3번 연속 0회).

## Terraform 실행기 (`terraform_runner`)

서비스가 사용자 계정에서 Terraform을 실행할 때 쓰는 모듈입니다(첫 대상은 공용 기반 `infra/user-account`).
`infra/user-account/tf.sh`(사람이 서비스 서버에서 하던 일)를 코드로 옮긴 것입니다.

```python
from pathlib import Path
from anyship_adapters import AwsEnvironment
from anyship_adapters.terraform_runner import TerraformError, TerraformRunner

runner = TerraformRunner(
    Path("infra/user-account"), plugin_cache_dir=Path("~/.terraform.d/plugin-cache").expanduser()
)
env = AwsEnvironment(
    ..., state_bucket="anyship-tfstate-<계정>-<리전>-<8자>"
)  # 온보딩 스택 출력 StateBucketName
try:
    changed = runner.plan(env, variables, log)  # 변경이 있으면 True. 아무것도 바꾸지 않는다
    runner.apply(env, variables, log)  # terraform apply -auto-approve
    env = runner.read_foundation(
        env, log
    )  # state의 출력으로 host, db_address, db_port, db_secret_arn을 채운 새 환경
except TerraformError as exc:
    exc.error  # AdapterError(code, message, hint, retryable)
    exc.tail  # 비밀을 가린 마지막 출력 15줄. 결과의 details에 넣는 용도
```

`variables`는 Terraform 변수 이름에서 값으로의 딕셔너리이고, **비밀이 아닌 값만** 넣습니다(임시 JSON 파일로 전달).
`log`는 어댑터와 같은 `LogEvent` 콜백으로, Terraform 출력 줄이 실시간으로 `step 1/2`(init), `2/2`(명령)로 들어옵니다.

**지키는 것**

- 자격 증명은 AssumeRole한 임시 자격 증명을 **하위 프로세스의 환경변수로만** 줍니다(`AwsAccess.temporary_credentials`). 파일, 명령줄,
  로그에 남지 않고, 서비스 서버의 `AWS_*` 환경과 인스턴스 메타데이터(`AWS_EC2_METADATA_DISABLED`)는 막습니다.
- state는 사용자 계정의 버킷에 `<환경ID>/foundation.tfstate`로 둡니다. 백엔드 리전은 버킷 이름에서 꺼냅니다.
- 실행마다 임시 `TF_DATA_DIR`을 쓰고 끝나면 지웁니다. 모듈 폴더는 `-chdir`로 제자리에서 읽으므로 동시 실행이 섞이지 않고,
  모듈이 읽는 형제 폴더(`../onprem-vm/scripts` 등)는 그대로 동작합니다(그래서 모듈은 저장소의 `infra/` 트리 안에 있어야 합니다).
- 같은 state(버킷, 환경 ID, state 이름)에는 한 번에 하나만 실행합니다. 한 프로세스 안에서는 먼저 막고, 여러 프로세스는 S3 잠금이 막습니다.
- 출력 줄은 비밀을 가려서 전달하고, 중단하거나 제한 시간(기본 45분)이 지나면 `terraform` 프로세스를 종료합니다.

**오류 코드** (`exc.error.code`)

| 코드 | 언제 | 재시도 |
|---|---|---|
| `foundation_missing` | `state_bucket`이 없거나, state가 비어 있음(기반이 아직 없음) | 아니요 |
| `terraform_module_missing` | 모듈 폴더에 `.tf` 파일이 없음 | 아니요 |
| `invalid_terraform_input` | 변수 이름이나 값 형식이 올바르지 않음 | 아니요 |
| `terraform_not_found` | 서비스 서버에 `terraform` 실행 파일이 없음 | 아니요 |
| `access_denied`, `service_credentials_unavailable`, `aws_unavailable` | 역할을 맡지 못함(`AwsAccess`와 같은 코드) | 코드에 따라 |
| `terraform_init_failed` | `init` 실패 | 아니요 |
| `terraform_plan_failed`, `terraform_apply_failed`, `terraform_output_failed` | 각 명령 실패 | 예 |
| `terraform_locked` | 같은 state에 다른 작업이 실행 중 | 예 |
| `terraform_timeout` | 제한 시간 초과로 종료. 만들어진 것은 다시 실행하면 이어서 진행 | 예 |
| `terraform_output_invalid` | 출력 JSON을 읽을 수 없음, 항목 누락, 값 검증 실패(틀린 필드 이름만 알리고 값은 싣지 않음) | 아니요 |

**시간** (서비스 서버 실측, 변경 없는 기반, provider 캐시가 데워진 상태)

| 호출 | 시간 |
|---|---|
| `plan` | 약 16초 |
| `read_foundation` | 약 10.5초 |

매 호출마다 AssumeRole과 `init`(백엔드 연결, provider 로드)을 하므로 변경이 없어도 약 10초가 듭니다. provider 캐시가 빈 첫 실행의 시간과
기반을 처음 만드는 `apply`(RDS 때문에 약 20분)의 시간은 이 실행기로 측정하지 않았습니다.

**시험**

```bash
python -m pytest infra/adapters    # 가짜 프로세스 기반 단위 테스트 + 진짜 하위 프로세스 1건

# 서비스 서버에서, 이미 만든 기반에 대해(읽기 전용: plan만 실행)
EXTERNAL_ID=... python -u infra/adapters/scripts/smoke_terraform_runner.py   --role-arn <역할 ARN> --state-bucket <StateBucketName> --var-file <test.tfvars>
```

`smoke_terraform_runner.py`는 변경 없음, 출력 변환, 중복 실행 거부, 틀린 계정 ID 거부, 비밀과 임시 파일 정리를 확인합니다.
`apply`는 실제 계정에서 이 실행기로 해 본 적이 없습니다(`plan`과 `output`만).

## 이미지 빌더 (`image_builder`)

서비스 서버의 로컬 도커에서 `<앱 이름>:<커밋 SHA>` 이미지를 빌드합니다. 서비스(A)가 PR이 머지된 커밋의 소스를 서비스 서버의
로컬 폴더로 준비해서 부르고, 어댑터의 `deploy`가 이 이미지를 `docker save | ssh docker load`로 호스트에 보냅니다(레지스트리 없음).
`Dockerfile`은 B가 만들어 레포에 넣어 둔 것을 씁니다.

```python
from pathlib import Path
from anyship_adapters.image_builder import BuildError, ImageBuilder

builder = ImageBuilder()
try:
    built = builder.build(
        Path("/path/to/merged/source"), "todo", "a1a1a1a", log
    )  # BuiltImage(image="todo:a1a1a1a", image_id="sha256:...")
    builder.prune(
        "todo", keep=5
    )  # 앱마다 최근 5개만 남기고 오래된 이미지를 지운다(서비스 서버 디스크 보호)
except BuildError as exc:
    exc.error  # AdapterError(code, message, hint, retryable)
    exc.tail  # 비밀을 가린 마지막 출력 15줄
```

**지키는 것**

- 원본 폴더를 건드리지 않고 **임시 복사본에서 빌드**합니다. `.git`과 `.env*`는 어느 깊이에서도 복사하지 않고(레포의 `.dockerignore`에 기대지 않음),
  심볼릭 링크는 따라가지 않고 링크 그대로 둡니다. 복사본이 200MB를 넘으면 거부합니다(제외된 파일은 크기에 세지 않음).
- 도커 프로세스에는 서비스 서버의 AWS 환경과 토큰을 넘기지 않고 입력을 기다리지 못하게 합니다.
- 이미지는 `linux/amd64`로 고정하고 빌드 뒤에 아키텍처를 확인합니다.
- 같은 (앱, SHA)는 동시에 빌드하지 않고, 제한 시간(기본 10분)이 지나거나 취소되면 도커 클라이언트를 종료합니다.
- `Dockerfile` 경로에 `..`나 절대 경로를 허용하지 않고, `Dockerfile`이 링크이거나 소스 밖을 가리키면 거부합니다.

**오류 코드** (`exc.error.code`)

| 코드 | 언제 | 재시도 |
|---|---|---|
| `invalid_build_input` | 앱 이름, SHA, 소스 폴더, `Dockerfile` 경로가 올바르지 않음 | 아니요 |
| `build_context_too_large` | 소스가 200MB를 넘음 | 아니요 |
| `docker_not_found` | 서비스 서버에 `docker`가 없음 | 아니요 |
| `docker_unavailable` | 도커 데몬에 연결할 수 없음(실행 중인지, 사용자가 `docker` 그룹인지) | 예 |
| `docker_build_failed` | 빌드가 실패했거나 빌드한 이미지를 확인할 수 없음. `exc.tail`에 마지막 출력 | 아니요 |
| `wrong_architecture` | 이미지가 `amd64`가 아님 | 아니요 |
| `build_in_progress` | 같은 (앱, SHA)를 이미 빌드 중 | 예 |
| `build_timeout` | 제한 시간 초과로 종료 | 예 |

**알려진 위험(수용한 것)**

- 이 빌드는 사용자 레포의 `Dockerfile`(임의의 `RUN` 명령)을 **서비스 서버에서** 실행합니다. 서비스 서버의 IAM 역할은 사용자 계정 AssumeRole 권한을 가집니다.
  **샘플 레포에만 쓰는 전제**이고, 격리된 빌드 환경은 확장 항목입니다.
- 서버에서 확인한 것: 빌드 중 `RUN` 단계는 **인스턴스 메타데이터(IAM 자격 증명)에 닿지 못합니다**(IMDSv2 필수, 홉 제한 1).
- 서버에서 확인한 것: 빌드 컨테이너는 서비스 포트(HTTPS 프록시용으로 `docker0` 주소에 바인딩한 `172.17.0.1:8000`)에 **닿습니다**.
  서비스 API는 로그인이 필요해서 당장 영향은 작지만, 인증이 필요 없는 엔드포인트(`/api/health`, `/api/config`, `/api/auth/github/start`)는 도달할 수 있습니다.
  빌드 전용 네트워크나 방화벽 규칙은 후속 과제입니다.

**시간** (서비스 서버 `t3.medium` 실측, 베이스 이미지를 미리 받아 둔 상태)

| 작업 | 시간 |
|---|---|
| B의 변환본(todo) 빌드, 콜드 | 약 8.4초 |
| 같은 소스를 다시 빌드(레이어 캐시) | 약 0.3초(같은 이미지 ID) |
| 베이스 이미지를 처음 받는 빌드 | 측정하지 않음 |
| 변환본을 `AwsAlwaysOnAdapter.deploy`로 AWS 호스트에 배포(이미지 전송, 앱 DB 준비, 기동, 마이그레이션, 헬스체크) | 약 9~10초 |

**시험**

```bash
python -m pytest infra/adapters        # 가짜 도커 기반 단위 테스트 + 진짜 하위 프로세스 1건

# 서비스 서버에서(진짜 도커). AWS에는 아무것도 하지 않는다
python -u infra/adapters/scripts/smoke_image_builder.py

# 서비스 서버에서. B의 변환본을 빌드하고 AWS 호스트에 배포했다가 destroy한다
EXTERNAL_ID=... python -u infra/adapters/scripts/smoke_sample_deploy.py   --role-arn <역할 ARN> --state-bucket <StateBucketName> --staging
```

`smoke_image_builder.py`는 `.git`·`.env*`가 이미지에 없음(B의 `.dockerignore`를 지우고도), `amd64`, 빌드 중 메타데이터 차단, `prune`을 확인합니다.
`smoke_sample_deploy.py`는 변환본이 `/healthz` 200, DB 읽기와 쓰기까지 동작하는지, `destroy` 뒤 앱 DB가 남아 이어지는지를 확인합니다.

**B의 변환본에 대해 알아 둘 점**: 변환본은 서버 시작 때 DB를 초기화하지 않고 `python -m app.migrate`로 분리했으며, 이 마이그레이션은 시드 데이터를 넣지 않습니다.
그래서 배포된 샘플은 빈 목록으로 시작합니다.

## 배포 파이프라인 (`deployer`, `foundation`)

서비스(A)가 부를 입구입니다. 소스 폴더와 커밋 SHA를 주면 이미지 빌드부터 공개 URL까지 한 번의 호출로 잇습니다.

```python
from anyship_adapters.deployer import Deployer
from anyship_adapters.foundation import FoundationSettings
from anyship_adapters.image_builder import ImageBuilder
from anyship_adapters.terraform_runner import TerraformRunner

deployer = Deployer(
    {"aws-always-on": aws_adapter, "onprem": onprem_adapter},
    ImageBuilder(),
    runner=TerraformRunner(
        Path("infra/user-account")
    ),  # runner와 foundation은 함께 줘야 한다(없으면 기반 단계를 건너뜀)
    foundation=FoundationSettings(
        service_server_ip="...", ssh_public_key="ssh-ed25519 ...", acme_email="..."
    ),
)
result = deployer.deploy(
    env, spec, Path("/path/to/merged/source"), commit_sha, secrets, log, set_name="aws-always-on"
)
result.ok, result.url, result.image_tag
result.details["stage"]  # 실패했을 때: spec / build / foundation / check / deploy
result.details[
    "foundation"
]  # 성공했을 때(aws): host, db_address, db_port, db_secret_arn. 서비스가 환경 레코드에 저장할 값
result.details["foundation_created"]  # 이번 호출이 기반을 새로 만들었으면 True
```

**순서**: 입력 검사 → 이미지 빌드 → (aws) 공용 기반 확인 → 연결 확인(`check`) → 배포 → 오래된 이미지 정리. 로그는 큰 단계(aws 1/5~5/5, 온프레미스 1/4~4/4)로
다시 번호를 붙여 흘려 보내고, 부품 안쪽 번호는 메시지 앞에 `[3/8]`로 남습니다.

- **입력 검사를 먼저 합니다.** 명세, 사용자 비밀 누락, SHA 형식, 앱 이름 길이(DB 이름 한도), 세트와 환경 종류 불일치는 빌드나 20분짜리 기반 생성을 시작하기 전에 `stage="spec"`으로 거절합니다.
- **공용 기반(`ensure_foundation`)**: state에 출력이 있으면 읽기만 하고(약 10초), **state가 비어 있을 때만** `apply`로 만듭니다(첫 배포만, 약 20분).
  `state_bucket`이 없거나 역할을 맡지 못했거나 잠금이 잡혀 있거나 출력이 이상하면 apply를 시작하지 않고 오류를 그대로 올립니다.
  서비스가 저장해 둔 기반 값이 있어도 매번 읽어서 최신 값을 씁니다(호스트가 바뀌었을 때 낡은 값을 쓰지 않으려는 것).
- **실패는 예외가 아니라 `ok=False` 결과**입니다. 실패한 단계 뒤는 실행하지 않고 정리도 하지 않습니다. 정리 실패는 경고만 남기고 배포 결과를 바꾸지 않습니다.
  예기치 않은 예외는 `deploy_pipeline_error`(재시도 가능)로 돌려주며 `details["exception"]`에는 예외 종류만 싣습니다(원문에 비밀이 섞일 수 있어서).
- 결과와 로그, 실패 상세에서 이번 배포의 비밀은 `***`로 가려집니다.
- 기반을 만들 때 쓰는 값(`FoundationSettings`)은 환경이 아니라 서비스가 아는 값입니다. 선택 변수(`vpc_cidr`, `host_instance_type`, `host_volume_size`,
  `db_instance_class`, `db_allocated_storage`)만 `overrides`로 바꿀 수 있습니다.

**시험**

```bash
python -m pytest infra/adapters        # test_foundation.py, test_deployer.py 포함

# 서비스 서버에서. 기반이 이미 있어야 한다(기반을 만들지 않고, apply는 막아 둔다)
EXTERNAL_ID=... python -u infra/adapters/scripts/smoke_deployer.py --role-arn <역할 ARN> --state-bucket <StateBucketName> --staging
```

`smoke_deployer.py`는 잘못된 입력의 조기 거절, 한 번의 호출로 5단계 로그와 기반 값이 결과에 실리는지, 공개 주소에서 읽기와 쓰기, `destroy`를 확인합니다(서버에서 13/13).
**아직 서버에서 확인하지 않은 것**: 기반이 없을 때 처음부터 만드는 경로(실제 `apply` 약 20분과 실시간 로그, `created=True`).

## 온프레미스를 서비스에 붙일 때 (연동 계약)

어댑터 쪽은 준비되어 있습니다. 서비스는 아래 호출만 부르면 됩니다(서비스 코드는 이 문서의 범위가 아닙니다). 서버를 사용자가 준비하는 방법은 [`infra/onprem-vm/README.md`](../onprem-vm/README.md)입니다.

### 서비스가 저장할 환경 값

`OnpremEnvironment`(비밀이 아니다): `env_id`(소문자 시작, 2~21자, 도메인과 DNS 이름에 쓰이는 **바뀌지 않는 값**), `host`(IP 또는 호스트 이름), `ssh_user`(기본 `deploy`), `ssh_port`(기본 22). SSH **개인 키는 환경 레코드가 아니라 서비스 설정**이다(`OnpremAdapter(key_path, ...)`). 지금은 모든 환경이 키 하나를 공유한다.

### 호출 순서

| 시점 | 호출 | 결과 |
|---|---|---|
| 등록 화면 | `render_setup_script(공개 키 한 줄, 사용자의 이메일)` | 사용자가 서버에서 `sudo bash`로 실행할 한 파일(Docker, `deploy` 계정, SSH 설정, Traefik). 공개 키와 이메일만 들어 있고 값은 형식을 검사한다(`SetupScriptError`) |
| 연결 확인 | `deployer.check(env, log, set_name="onprem")` | SSH, Docker, Traefik, 공개 IP를 확인. 성공하면 `details["public_ip"]`. 실패하면 오류 코드와 힌트 |
| 배포 | `deployer.deploy(env, spec, source_dir, commit_sha, secrets, log, set_name="onprem")` | 이미지 빌드 → 연결 확인 → 배포. **`runner`와 `foundation` 없이** 만든 `Deployer`를 쓰면 4단계이고, 공용 기반 단계는 없다. DNS는 어댑터가 첫 배포에서 맞춘다(아래) |
| 앱 삭제 | `deployer.destroy(env, app, log, set_name="onprem")` | 앱 컨테이너, 볼륨, 서버의 앱 폴더. 앱 DB와 DNS는 남는다 |
| 환경 삭제 | `deployer.remove_environment(env, log, set_name="onprem")` | 환경의 DNS 레코드를 지운다(서버에는 접속하지 않음). **이 환경에 남은 앱이 없을 때만** 부른다(어댑터는 앱을 세지 않는다) |

`Deployer`를 만드는 모양:

```python
dns = WildcardRecords(
    scope="onprem", base_domain="anyship.cloud"
)  # 서비스 서버 역할의 Route 53(IAM 적용 완료)
adapter = OnpremAdapter(
    Path("/home/ec2-user/.ssh/onprem_deploy"), base_domain="anyship.cloud", dns=dns
)
deployer = Deployer({"onprem": adapter}, ImageBuilder())  # runner, foundation 없음 → 기반 단계 없음
```

AWS 세트와 함께 쓰려면 `{"aws-always-on": aws_adapter, "onprem": adapter}`로 한 `Deployer`에 같이 넣고 `set_name`으로 고른다. 환경 종류와 세트가 맞지 않으면 어댑터를 부르기 전에 `set_not_supported`로 거절된다.

### DNS가 만들어지는 때

레코드는 앱마다가 아니라 **환경(서버)마다 하나**(`*.<환경ID>.onprem.<도메인>` → 서버의 공인 IP)다. `OnpremAdapter`에 `dns`를 주면 **배포 때마다** 서버의 공인 IP로 맞추므로(멱등) IP가 바뀌어도 스스로 고쳐진다. 등록 시점에 미리 만들 필요는 없다. 레코드를 지우는 것은 `remove_environment`뿐이다.

### 오류 코드

| 코드 | 언제 | 재시도 |
|---|---|---|
| `ssh_unreachable` | 서버에 SSH로 닿지 못함(주소, 포트, 방화벽, 준비 스크립트) | 예 |
| `ssh_command_failed` | 접속은 되지만 명령이 실패 | 아니요 |
| `docker_missing` | Docker 또는 Compose가 없음 | 아니요 |
| `proxy_not_ready` | Traefik이 실행 중이지 않음 | 아니요 |
| `public_ip_unknown` | 서버의 공인 IPv4를 알 수 없음 | 예 |
| `dns_change_failed`, `dns_zone_not_found`, `invalid_dns_input` | DNS를 맞추지 못함(권한, 영역, 비공인 IP) | 코드에 따라 |
| `set_not_supported` | 환경 종류와 맞지 않는 세트 | 아니요 |
| `check_pipeline_error`, `destroy_pipeline_error`, `remove_environment_pipeline_error` | 예기치 않은 예외(원문은 싣지 않고 `details["exception"]`에 종류만) | 예 |
| `remove_environment_unsupported` | 환경 단위 정리가 없는 세트(AWS)에 `remove_environment`를 부름 | 아니요 |

배포 중 오류(`image_transfer_failed`, `server_write_failed`, `container_start_failed`, `migration_failed`, `healthcheck_failed` 등)는 `deploy` 결과의 `error`로 나오고, 실패한 큰 단계는 `details["stage"]`다.

### 서비스 쪽에서 바뀌어야 할 것 (참고, 이 저장소의 서비스 코드는 아직 건드리지 않았다)

- 환경 레코드: 온프레미스 서버 정보를 저장할 곳(지금 서비스에는 AWS 환경 테이블뿐).
- 배포 대상(`deployments`)이 AWS 환경에만 묶여 있다. 환경 종류별로 가리킬 수 있어야 하고, 배포 가능 세트(`REAL_SETS`)에 `onprem`이 들어가야 한다.
- 실행기가 `OnpremAdapter`와 `WildcardRecords(scope="onprem")`를 조립해야 한다.
- 화면: 서버 정보 입력, 준비 스크립트와 안내, 연결 확인 결과.

### 시험

```bash
python -m pytest infra/adapters   # 온프레미스 어댑터, DNS, 준비 스크립트, Deployer 진입점 포함

# 서비스 서버에서, 실제 온프레미스 시험 서버로(Route 53은 건드리지 않음)
python scripts/smoke_onprem.py --host <서버 IP> --env-id demo --staging
# DNS까지(새 환경 ID로만: 레코드를 만들고 지운다)
python scripts/smoke_onprem.py --host <서버 IP> --env-id dnstest --dns --staging
```

## 데이터 이전 (`data_transfer`)

앱의 **DB(PostgreSQL)** 를 한 환경에서 다른 환경으로 옮긴다. 온프레미스 서버에서 AWS로(또는 반대로) 옮길 때 쓰고, S3 같은 중간 저장소는 필요 없다.
원본 서버의 `pg_dump`를 서비스 서버가 **그대로 중계**해서 대상 서버의 `pg_restore`로 흘려보낸다(이미지를 보내는 `docker save | docker load`와 같은 방식).
디스크나 메모리에 덤프를 모으지 않는다. 대신 서비스 서버의 네트워크를 지나가므로 작은 DB용이다(기본 한도 1 GiB, `max_bytes`).

```python
result = deployer.transfer_data(
    onprem_env, aws_env, "todo", log, source_set="onprem", target_set="aws-always-on"
)
# result.ok, result.details == {"bytes": ..., "tables": ..., "rows": ..., "source_stopped": True}
```

**호출 전에 할 일**: 대상 환경에 같은 앱을 먼저 배포한다(빈 DB로). 복원은 `--clean --if-exists`라서 대상의 기존 테이블을 원본의 것으로 바꾼다.

| 순서 | 단계 | 하는 일 |
|---|---|---|
| 1 | 준비 확인 | 두 서버에 접속, 앱이 둘 다 있는지, 원본 DB 크기(한도), 대상 DB 접속 |
| 2 | 쓰기 중지 | 두 환경의 **웹 컨테이너만** 멈춘다(DB는 켜 둔다). 멈춘 것은 실패하면 모두 되돌린다 |
| 3 | 데이터 복사 | `pg_dump --format=custom --no-owner --no-privileges` → `pg_restore --clean --if-exists --single-transaction` |
| 4 | 검증 | 테이블별 `count(*)`를 원본과 대상에서 세어 비교한다 |
| 5 | 마무리 | 대상 앱을 시작한다. **원본 앱은 멈춘 채로 둔다**(`source_stopped`). 사용자가 새 주소에서 확인한 뒤 원본을 지우고, 문제가 있으면 원본을 다시 켠다 |

- 복원은 한 트랜잭션이라 **실패하면 대상 DB는 그대로**이고, 원본 앱은 다시 시작한다. 데이터는 옮겼는데 대상 앱이 시작되지 못하면 원본이 다시 서게 한다(양쪽이 모두 멈추지 않게). 행 수가 맞지 않으면 원본을 다시 켜고 의심스러운 대상 앱은 멈춘 채로 둔다.
- **옮기지 않는 것**: 컨테이너 볼륨의 파일, 환경마다 따로 생성되는 비밀 값(`SECRET_KEY` 등, 옮기면 기존 로그인 세션이 풀린다), 앱 주소(환경이 다르면 주소도 다르다. `<앱>.<환경ID>.<aws|onprem>.<도메인>`).
- **비밀은 명령줄에 쓰지 않는다.** 온프레미스는 `db` 컨테이너 안에서 소켓으로 접속하고, RDS는 호스트의 `app.env`를 읽어 `DATABASE_URL`을 환경변수로만 컨테이너에 넘긴다(`rds_admin`과 같은 원칙). SQL은 표준 입력으로 보낸다.
- 어댑터는 `data_endpoint(env, app)`로 접속 방법을 돌려준다(`OnpremAdapter`는 `db` 컨테이너, `AwsAlwaysOnAdapter`는 공용 RDS). 두 환경이 이 메서드를 가지면 어느 방향이든 옮길 수 있다.
- 두 PostgreSQL 버전은 호환되어야 한다(지금 온프레미스 `db`는 16, RDS는 16 계열이다). 새 세트를 추가할 때 이 점을 확인한다.

`Deployer.check_transfer_source(env, app, log, set_name=...)`는 옮기기 전에 원본만 미리 확인한다(접속, 앱, DB 크기. 아무것도 멈추지 않는다). 서비스는 이것으로 대상을 새로 배포하기 *전에* 옮길 수 없는 DB를 걸러낸다.

서비스 연결은 `service/README.md`의 "환경 이전"을 본다(작업 종류 `migrate`, `destroy_previous`, 화면 버튼). 아직 없는 것: 성공 뒤 "원본 앱 다시 켜기"(되돌리기) 버튼.

## 오류 코드 (현재)

mock이 내는 코드이고, 실제 어댑터가 생기면 더 늘어납니다

| 코드 | 언제 | `retryable` |
|---|---|---|
| `ssh_unreachable` | 온프레미스 서버에 SSH로 접속할 수 없음 | 예 |
| `assume_role_denied` | 사용자 AWS 계정의 역할을 사용할 수 없음 | 예 |
| `invalid_spec` | 명세의 앱 이름이 올바르지 않음 | 아니요 |
| `invalid_image_tag` | 이미지 태그가 커밋 SHA 형식(16진수 7~40자)이 아님 | 아니요 |
| `set_not_supported` | 환경과 맞지 않는 세트 | 아니요 |
| `unsupported_backing_service` | `object_storage`를 쓰는 앱(아직 미지원) | 아니요 |
| `app_not_found` | 배포되지 않은 앱을 되돌리려 함 | 아니요 |
| `container_start_failed` | 새 컨테이너가 시작되지 않음 | 예 |
| `healthcheck_failed` | 앱이 `/healthz`에 응답하지 않음 | 예 |
| `app_not_found` | (이전) 원본이나 대상에 앱이 배포되어 있지 않음 | 아니요 |
| `db_too_large` | 원본 DB가 한도(`max_bytes`)보다 큼 | 아니요 |
| `source_db_unavailable`, `target_db_unavailable` | 원본이나 대상 DB에 접속하지 못함 | 예 |
| `quiesce_failed` | 쓰기를 막으려고 앱을 멈추지 못함 | 예 |
| `dump_failed`, `restore_failed`, `restore_timeout` | 내보내기나 복원이 실패(복원 실패면 대상은 그대로) | 예 |
| `verify_failed`, `verify_mismatch` | 행 수를 확인하지 못했거나 원본과 다름(대상 앱은 멈춘 채로 둠) | 예 |
| `target_start_failed` | 데이터는 옮겼지만 대상 앱을 시작하지 못함(원본 앱을 다시 시작한다) | 예 |
| `same_environment`, `transfer_not_supported` | 원본과 대상이 같거나, 이전을 지원하지 않는 조합 | 아니요 |
| `transfer_pipeline_error` | 예상하지 못한 오류(내용은 로그에 싣지 않는다) | 예 |

## mock 어댑터

실제 환경 없이 서비스를 개발하고 시험하는 용도입니다. 

```python
MockAdapter()  # 모든 것이 성공
MockAdapter("check_fails")  # check 실패
MockAdapter("deploy_fails")  # 4단계(앱 시작)에서 실패
MockAdapter("unhealthy")  # 5단계(헬스체크)에서 실패
MockAdapter(delay=1.0)  # 단계마다 1초 대기(진행 화면 확인용)
```

- 앱 정보는 **메모리에만** 있어서 프로세스를 다시 시작하면 사라집니다.
- 실패한 배포는 앱을 남기지 않습니다.

## 비밀이 새지 않게 하는 방법

- 어댑터는 로그와 결과가 나가기 전에 비밀을 걸러냅니다. 이번 배포의 비밀 값은 나타나는 모든 곳에서
  `***`로 바뀌고, 클라우드 키·토큰·개인 키·URL 안의 비밀번호 같은 **알려진 모양**도 바뀝니다.
- 서비스도 `secrets`를 DB와 로그에 남기지 않아야 합니다.
- 4자보다 짧은 비밀은 걸러내지 않습니다.

## 테스트

패키지 폴더에서 실행합니다.

```bash
cd infra/adapters
python -m pip install -e ".[test]"
python -m pytest
```

서비스가 실제로 쓰는 버전 조합에서 확인하려면, 서비스의 의존성을 먼저 설치하고 어댑터는 의존성 없이
추가합니다.

```bash
python -m pip install -r service/requirements.txt
python -m pip install --no-deps -e infra/adapters pytest
python -m pytest infra/adapters
```

## 남은 것

- `*.aws` DNS 자동화
- 기반을 처음부터 만드는 경로(`ensure_foundation`의 `apply`, 약 20분과 실시간 진행 로그)를 실제 계정에서 시험(지금은 가짜 실행기 시험과 `plan`, `output`만 확인)
- 서비스 연동: 소스 가져오기, 환경 레코드에 기반 값과 `state_bucket` 저장, 배포 실행기와 잠금, 화면
- Lambda 세트(`aws-serverless`)
- 격리된 빌드 환경(지금은 서비스 서버에서 사용자 `Dockerfile`을 실행하므로 샘플 레포에만 쓸 것), 빌드 컨테이너가 서비스 포트에 닿는 것 막기
- 앱의 비밀을 Secrets Manager에 보관(현재는 호스트의 `app.env`). 호스트 교체에도 비밀을 유지하고, 사용자 입력 비밀을
  재배포마다 다시 넘기지 않아도 되게 하려면 필요
- 환경별 SSH 키, 앱 삭제 때 앱 DB 삭제 같은 확장 항목
- 오류 코드 추가
- 데이터 이전을 실제 환경에서 시험: `scripts/smoke_transfer.py`(진짜 Postgres, 서비스 서버에서)와 서버 두 대(SSH 중계, RDS 쪽 명령). 서비스 연결(작업 종류 `transfer`, 화면)

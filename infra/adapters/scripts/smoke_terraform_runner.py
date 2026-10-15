"""실제 계정에서 TerraformRunner를 시험하는 수동 시험(자동 테스트가 아니다). 만들거나 바꾸는 것은 없다(plan만).

서비스 서버에서 실행한다. 공용 기반(infra/user-account)이 이미 만들어져 있어야 한다. 확인하는 것:
  1) plan: 이미 적용한 기반에 대해 변경이 없다
  2) read_foundation: 출력이 기반 필드로 변환된다
  3) 같은 환경에 두 번째 실행을 동시에 시작하면 거부된다
  4) 틀린 계정 ID로 plan하면 provider가 막고, 그 실패가 오류 코드로 보고된다
  5) 로그와 오류에 External ID가 없고, 임시 폴더가 남지 않는다

    EXTERNAL_ID=... python infra/adapters/scripts/smoke_terraform_runner.py \\
      --role-arn <역할 ARN> --state-bucket <StateBucketName> --env-id test \\
      --var-file ~/team-bronze/infra/user-account/test.tfvars

External ID는 명령줄이 아니라 환경변수로만 받는다. --var-file은 `키 = "값"`, 숫자, true/false의 단순한 한 줄 형식만
읽는다(주석 줄과 비어 있는 줄은 무시). 모듈 폴더는 이 저장소의 infra/user-account이고, 시험하는 코드와 적용한 코드가 같아야
plan이 변경 없음으로 끝난다. 실행마다 provider를 내려받으므로 --plugin-cache(기본 ~/.terraform.d/plugin-cache)를 쓴다.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

import smoke_onprem as base
from smoke_onprem import expect, timed

from anyship_adapters import AwsEnvironment
from anyship_adapters.terraform_runner import TerraformError, TerraformRunner

TFVAR = re.compile(r'^\s*([a-z_][a-z0-9_]*)\s*=\s*("[^"\\]*"|-?\d+|true|false)\s*(?:#.*)?$')
INTERESTING = ("Plan:", "No changes", "Error", "error", "Apply complete", "Initializing")
collected: list[str] = []  # 모든 로그 문구(비밀이 없는지 마지막에 검사한다)


def read_tfvars(path: str) -> dict:
    values = {}
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        found = TFVAR.match(line)
        if found:
            values[found.group(1)] = json.loads(found.group(2))  # 위 형식은 JSON 값과 같다
    return values


def make_log(verbose: bool):
    def log(event) -> None:
        collected.append(event.message)
        if (
            verbose
            or event.message.startswith("terraform ")
            or any(word in event.message for word in INTERESTING)
        ):
            print(f"    [{event.step}/{event.total}] {event.message}")

    return log


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--state-bucket", required=True, help="온보딩 스택 출력 StateBucketName")
    parser.add_argument("--var-file", required=True, help="기반을 만들 때 쓴 test.tfvars")
    parser.add_argument("--env-id", default="test")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument(
        "--module", default=str(Path(__file__).resolve().parents[2] / "user-account")
    )
    parser.add_argument(
        "--plugin-cache", default=str(Path.home() / ".terraform.d" / "plugin-cache")
    )
    parser.add_argument(
        "--outputs", help="`terraform output -json`을 저장한 파일(있으면 host와 비교한다)"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Terraform의 모든 출력 줄을 보여 준다"
    )
    args = parser.parse_args()
    external_id = os.environ.get("EXTERNAL_ID")
    if not external_id:
        print("EXTERNAL_ID 환경변수를 지정하세요(명령줄 인자로는 받지 않습니다).")
        return 2

    cache = Path(args.plugin_cache)
    cache.mkdir(parents=True, exist_ok=True)
    env = AwsEnvironment(
        env_id=args.env_id,
        role_arn=args.role_arn,
        external_id=external_id,
        region=args.region,
        state_bucket=args.state_bucket,
    )
    variables = read_tfvars(args.var_file)
    runner = TerraformRunner(Path(args.module), plugin_cache_dir=cache)
    log = make_log(args.verbose)
    print(f"변수 {len(variables)}개: {', '.join(sorted(variables))}")

    def attempt(label, call):
        try:
            return timed(call)
        except TerraformError as exc:
            print(f"   -> 실패 {exc.error.code}: {exc.error.message}")
            if exc.tail:
                print("      마지막 출력:\n      " + exc.tail.replace("\n", "\n      "))
            collected.extend([exc.error.message, exc.tail])
            return exc

    print("1) plan (이미 적용한 기반에는 변경이 없어야 한다)")
    changed = attempt("plan", lambda: runner.plan(env, variables, log))
    expect("plan이 실패 없이 끝난다", not isinstance(changed, TerraformError))
    expect("변경이 없다(no change)", changed is False)

    print("2) read_foundation")
    filled = attempt("output", lambda: runner.read_foundation(env, log))
    ok = isinstance(filled, AwsEnvironment)
    expect(
        "출력이 기반 필드로 변환된다",
        ok and all((filled.host, filled.db_address, filled.db_port, filled.db_secret_arn)),
    )
    if ok:
        print(
            f"      host={filled.host} db={filled.db_address.split('.')[0]} port={filled.db_port}"
        )
        if args.outputs:
            saved = json.loads(Path(args.outputs).read_text())
            expect("저장해 둔 출력과 host가 같다", saved["host_public_ip"]["value"] == filled.host)

    print("3) 같은 환경에 두 번째 실행을 동시에 시작")
    first = threading.Thread(
        target=lambda: attempt("첫 번째", lambda: runner.plan(env, variables, lambda e: None))
    )
    first.start()
    time.sleep(2)
    second = attempt("두 번째", lambda: runner.plan(env, variables, log))
    expect(
        "두 번째가 terraform_locked로 거부된다",
        isinstance(second, TerraformError) and second.error.code == "terraform_locked",
    )
    first.join()
    expect(
        "첫 번째가 끝난 뒤에는 다시 실행할 수 있다(잠금이 풀렸다)",
        not isinstance(
            attempt("세 번째", lambda: runner.read_foundation(env, lambda e: None)), TerraformError
        ),
    )

    print("4) 틀린 계정 ID로 plan (provider가 막아야 한다)")
    wrong = attempt(
        "틀린 계정", lambda: runner.plan(env, {**variables, "account_id": "000000000000"}, log)
    )
    expect(
        "terraform_plan_failed로 보고된다",
        isinstance(wrong, TerraformError) and wrong.error.code == "terraform_plan_failed",
    )
    expect(
        "계정 고정(allowed_account_ids) 메시지가 마지막 출력에 있다",
        isinstance(wrong, TerraformError) and "not allowed" in wrong.tail,
    )

    print("5) 비밀과 임시 파일")
    text = "\n".join(collected)
    expect("로그와 오류에 External ID가 없다", external_id not in text)
    expect(
        "로그와 오류에 자격 증명 변수 이름의 값 대입이 없다",
        "AWS_SECRET_ACCESS_KEY=" not in text and "AWS_SESSION_TOKEN=" not in text,
    )
    left = list(Path(tempfile.gettempdir()).glob("anyship-tf-*"))
    expect("임시 폴더가 남지 않았다", not left)

    failed = [label for label, passed in base.results if not passed]
    print(f"\n결과: {len(base.results) - len(failed)}/{len(base.results)} OK")
    for label in failed:
        print("  FAIL:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

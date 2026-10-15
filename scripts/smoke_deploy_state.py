"""서비스 DB의 실제 AWS 환경 레코드로 `service/app/deploy_state.py`를 시험하는 수동 시험(자동 테스트가 아니다).

서비스 서버에서 실행한다. 서비스 DB(APP_DATABASE_URL, 없으면 서비스의 기본값)를 읽고 **쓴다**. 환경 레코드는 `CONNECTED`여야 하고,
공용 기반(infra/user-account)이 이미 만들어져 있어야 한다. 확인하는 것:
  1) env_id가 정해진다(레코드에 이미 있으면 그대로)
  2) 어댑터 환경으로 바뀐다(External ID는 DB에서 읽는다. 명령줄과 화면에는 나오지 않는다)
  3) 진짜 AWS의 describe_stacks로 StateBucketName을 읽고, 저장된 값이 있으면 같은지 본다
  4) ensure_state_bucket이 비어 있는 state_bucket을 채운다(이미 있으면 AWS를 부르지 않는다)
  5) TerraformRunner.read_foundation의 결과를 save_foundation으로 저장하고, 다시 읽은 값이 같은지 본다
  6) 로그와 출력에 External ID가 없다

    cd service && python ../scripts/smoke_deploy_state.py --environment-id <서비스 DB의 aws_environments.id> [--env-id test]

--env-id는 레코드에 env_id가 없을 때만 쓴다(먼저 만든 기반이 `test`일 때). 주지 않으면 레코드 ID에서 정한다.
이미 만든 기반의 env_id와 다르면 기반을 못 찾으므로(state의 키가 env_id다), 켜 둔 테스트 계정은 `--env-id test`로 실행한다.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from dotenv import load_dotenv  # noqa: E402

from anyship_adapters.aws_access import AwsAccess, AwsAccessError  # noqa: E402
from anyship_adapters.terraform_runner import TerraformError, TerraformRunner  # noqa: E402
from app.config import DEFAULT_DATABASE_URL  # noqa: E402
from app.db import AwsEnvironment, database  # noqa: E402
from app.deploy_state import (  # noqa: E402
    FOUNDATION_FIELDS,
    DeployStateError,
    adapter_environment,
    assign_env_id,
    ensure_state_bucket,
    save_foundation,
)

results: list[tuple[str, bool]] = []
collected: list[str] = []


def expect(label: str, passed: bool) -> None:
    results.append((label, bool(passed)))
    print(f"   {'OK  ' if passed else 'FAIL'} {label}")


class CountingAccess:
    """진짜 AwsAccess를 감싸서 AWS를 몇 번 불렀는지 센다."""

    def __init__(self):
        self.real, self.reads = AwsAccess(), 0

    def read_state_bucket(self, env, stack_name):
        self.reads += 1
        return self.real.read_state_bucket(env, stack_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--env-id", help="레코드에 env_id가 없을 때 쓸 값(예: test)")
    parser.add_argument(
        "--plugin-cache", default=str(Path.home() / ".terraform.d" / "plugin-cache")
    )
    args = parser.parse_args()

    load_dotenv(ROOT / "service" / ".env.github.local")
    engine, sessions = database(os.getenv("APP_DATABASE_URL") or DEFAULT_DATABASE_URL)
    try:
        with sessions() as session:
            row = session.get(AwsEnvironment, args.environment_id)
            if row is None:
                print("그 ID의 환경 레코드가 없습니다.")
                return 2
            print(f"환경: {row.name} ({row.region}), 상태 {row.status}")
            expect("환경이 CONNECTED다", row.status == "CONNECTED")
            if row.status != "CONNECTED":
                return 1
            external_id = row.external_id

            print("1) env_id")
            if row.env_id is None and args.env_id:
                row.env_id = args.env_id
                session.commit()
            env_id = assign_env_id(session, row)
            print(f"      env_id = {env_id}")
            expect("env_id가 정해졌다", bool(env_id))
            if args.env_id and env_id != args.env_id:
                print(f"      (레코드에 이미 {env_id}가 있어서 --env-id는 쓰지 않았습니다)")

            print("2) 어댑터 환경")
            env = adapter_environment(row)
            expect("어댑터 환경으로 바뀐다", env.env_id == env_id and env.role_arn == row.role_arn)

            print("3) 진짜 AWS: describe_stacks로 StateBucketName 읽기")
            bucket = AwsAccess().read_state_bucket(env, row.stack_name)
            print(f"      {bucket}")
            expect(
                "버킷 이름이 규칙에 맞고 이 계정의 것이다",
                bucket.split("-")[2] == row.role_arn.split(":")[4],
            )
            if row.state_bucket:
                expect("저장된 state_bucket과 같다", row.state_bucket == bucket)

            print("4) ensure_state_bucket")
            counting = CountingAccess()
            had = bool(row.state_bucket)
            expect("state_bucket이 채워진다", ensure_state_bucket(session, row, counting) == bucket)
            expect(
                "비어 있었으면 AWS를 한 번 부르고, 있었으면 부르지 않는다",
                counting.reads == (0 if had else 1),
            )
        with sessions() as session:
            expect(
                "DB에 저장되어 있다",
                session.get(AwsEnvironment, args.environment_id).state_bucket == bucket,
            )

            print("5) read_foundation의 결과를 저장하기")
            row = session.get(AwsEnvironment, args.environment_id)
            runner = TerraformRunner(
                ROOT / "infra" / "user-account", plugin_cache_dir=Path(args.plugin_cache)
            )
            found = runner.read_foundation(
                adapter_environment(row), lambda event: collected.append(event.message)
            )
            fields = {name: getattr(found, name) for name in FOUNDATION_FIELDS}
            expect("기반 값 4개가 모두 있다", all(item is not None for item in fields.values()))
            save_foundation(session, row, fields)
        with sessions() as session:
            saved = session.get(AwsEnvironment, args.environment_id)
            expect(
                "다시 읽은 값이 저장한 값과 같다",
                {name: getattr(saved, name) for name in FOUNDATION_FIELDS} == fields,
            )
            expect(
                "저장된 값으로 만든 어댑터 환경에 호스트가 들어 있다",
                adapter_environment(saved).host == fields["host"],
            )

        print("6) 비밀")
        expect("로그에 External ID가 없다", not any(external_id in line for line in collected))
    except (DeployStateError, TerraformError, AwsAccessError) as exc:
        error = getattr(exc, "error", exc)
        print(
            f"   -> 실패 {getattr(error, 'code', type(exc).__name__)}: {getattr(error, 'message', exc)}"
        )
        expect("시험이 중간에 멈추지 않았다", False)
    finally:
        engine.dispose()

    failed = [label for label, passed in results if not passed]
    print(f"\n결과: {len(results) - len(failed)}/{len(results)} OK")
    for label in failed:
        print("  FAIL:", label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

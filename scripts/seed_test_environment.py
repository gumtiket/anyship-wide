"""시험용 SQLite DB에 연결된(CONNECTED) AWS 환경 한 건을 만든다(서비스 운영 DB는 건드리지 않는다).

화면(GitHub 로그인)을 거치지 않고 실제 계정으로 `smoke_deploy_state.py` 같은 서버 시험을 하려는 용도다. 사용자, 워크스페이스,
환경 레코드를 새 SQLite 파일에 넣는다. External ID는 명령줄이 아니라 환경변수 EXTERNAL_ID나 입력창(화면에 보이지 않음)으로만 받는다.

    cd service && python ../scripts/seed_test_environment.py --database ~/anyship-test.db \\
      --role-arn <역할 ARN> --stack-name <온보딩 스택 이름>

끝나면 환경 ID와, 그 DB로 시험을 돌리는 명령을 보여 준다. 이미 있는 파일에는 쓰지 않는다.
"""

import argparse
import getpass
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from app.aws_validation import role_account_id  # noqa: E402
from app.db import AwsEnvironment, Base, Membership, User, Workspace, database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, help="새로 만들 SQLite 파일 경로")
    parser.add_argument("--role-arn", required=True)
    parser.add_argument(
        "--stack-name", required=True, help="사용자 계정의 온보딩 CloudFormation 스택 이름"
    )
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--name", default="테스트 계정")
    parser.add_argument(
        "--env-id",
        help="이미 만든 공용 기반의 env_id(켜 둔 테스트 계정은 test). 비우면 배포할 때 정해진다",
    )
    args = parser.parse_args()

    path = Path(args.database).expanduser().resolve()
    if path.exists():
        print(f"{path}이(가) 이미 있습니다. 새 파일 이름을 지정하세요.")
        return 1
    try:
        account = role_account_id(args.role_arn)
    except ValueError:
        print("--role-arn이 IAM 역할 ARN 형식이 아닙니다(arn:aws:iam::<계정>:role/<이름>).")
        return 2
    external_id = os.environ.get("EXTERNAL_ID") or getpass.getpass(
        "External ID(화면에 보이지 않음): "
    )
    if len(external_id) < 16:
        print("External ID가 너무 짧습니다.")
        return 2

    engine, sessions = database(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    identifier, now = str(uuid.uuid4()), int(time.time())
    with sessions() as session:
        # github_id는 양수여야 한다(음수 ID는 데모 모드 전용 세션으로 취급되어 일반 모드에서 거절된다).
        session.add_all(
            [
                User(
                    id="local-test-user",
                    github_id=900_000_001,
                    login="local-test",
                    name="로컬 시험",
                ),
                Workspace(id="local-test-workspace", name="로컬 시험"),
            ]
        )
        session.flush()
        session.add(
            Membership(user_id="local-test-user", workspace_id="local-test-workspace", role="owner")
        )
        session.add(
            AwsEnvironment(
                id=identifier,
                workspace_id="local-test-workspace",
                created_by="local-test-user",
                request_id=identifier,
                name=args.name,
                region=args.region,
                external_id=external_id,
                template_url="https://example.s3.amazonaws.com/not-used.yaml",
                service_role_arn="arn:aws:iam::000000000000:role/not-used",
                stack_name=args.stack_name,
                role_name=args.role_arn.rsplit("/", 1)[-1],
                status="CONNECTED",
                submitted_role_arn=args.role_arn,
                role_arn=args.role_arn,
                aws_account_id=account,
                created_at=now,
                expires_at=now + 86400,
                verified_at=now,
                env_id=args.env_id,
            )
        )
        session.commit()
    engine.dispose()
    print(f"만들었습니다: {path}\n환경 ID: {identifier}\n")
    print("시험 명령:")
    print(
        f"  APP_DATABASE_URL=sqlite:///{path.as_posix()} python ../scripts/smoke_deploy_state.py "
        f"--environment-id {identifier} --env-id test"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""이미 있는 테스트 AWS 계정(역할)을 서비스 DB의 한 사용자 워크스페이스에 '연결된(CONNECTED)' 환경으로 등록한다.

화면의 환경 등록은 새 External ID로 새 CloudFormation 스택을 만들게 하는데, 테스트 계정에는 같은 이름의 역할이 이미 있어서 충돌한다.
그래서 시험에서는 기존 역할의 정보를 직접 넣는다. 등록하기 전에 **실제로 그 역할을 맡을 수 있는지 확인**해서, 틀린 External ID를 저장하지 않는다.

서비스 서버에서 실행한다. **서비스의 실제 DB에 쓴다**(환경 한 건 추가). 사용자가 먼저 app.anyship.cloud에 GitHub로 로그인해 있어야 한다.
External ID는 명령줄이 아니라 환경변수 EXTERNAL_ID나 입력창(화면에 보이지 않음)으로만 받는다.

    cd service && python ../scripts/attach_test_environment.py --login <GitHub 로그인 이름> \\
      --role-arn <역할 ARN> --stack-name <온보딩 스택 이름> --env-id test

같은 역할이 이미 등록되어 있으면 아무것도 바꾸지 않고 그 환경의 ID를 알려 준다.
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

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.aws_validation import role_account_id  # noqa: E402
from app.config import DEFAULT_DATABASE_URL  # noqa: E402
from app.db import AwsEnvironment, Membership, User, database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", required=True, help="서비스에 로그인한 GitHub 로그인 이름")
    parser.add_argument("--role-arn", required=True)
    parser.add_argument(
        "--stack-name", required=True, help="사용자 계정의 온보딩 CloudFormation 스택 이름"
    )
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--name", default="테스트 계정")
    parser.add_argument(
        "--env-id",
        default="test",
        help="공용 기반의 env_id(이 값으로 도메인과 Terraform state가 정해진다)",
    )
    parser.add_argument(
        "--skip-check", action="store_true", help="역할을 실제로 맡아 보는 확인을 건너뛴다(시험용)"
    )
    args = parser.parse_args()

    try:
        account = role_account_id(args.role_arn)
    except ValueError:
        print("--role-arn이 IAM 역할 ARN 형식이 아닙니다(arn:aws:iam::<계정>:role/<이름>).")
        return 2
    load_dotenv(ROOT / "service" / ".env.github.local")
    url = os.getenv("APP_DATABASE_URL") or DEFAULT_DATABASE_URL
    shown = make_url(url)
    print(
        f"대상 DB: {shown.get_backend_name()} {shown.host or shown.database}"
    )  # 비밀번호는 출력하지 않는다
    engine, sessions = database(url)
    try:
        with sessions() as session:
            user = session.scalar(select(User).where(User.login == args.login))
            if user is None:
                print(
                    "그 GitHub 로그인으로 가입한 사용자가 없습니다. 먼저 app.anyship.cloud에 로그인하세요."
                )
                return 2
            workspace_id = session.scalar(
                select(Membership.workspace_id).where(Membership.user_id == user.id)
            )
            existing = session.scalar(
                select(AwsEnvironment).where(
                    AwsEnvironment.workspace_id == workspace_id,
                    AwsEnvironment.role_arn == args.role_arn,
                )
            )
            if existing is not None:
                print(
                    f"이미 등록되어 있습니다. 환경 ID: {existing.id} (상태 {existing.status}, env_id {existing.env_id})"
                )
                return 0

            external_id = os.environ.get("EXTERNAL_ID") or getpass.getpass(
                "External ID(화면에 보이지 않음): "
            )
            if len(external_id) < 16:
                print("External ID가 너무 짧습니다.")
                return 2
            if not args.skip_check:
                from anyship_adapters import AwsEnvironment as AdapterEnvironment
                from anyship_adapters.aws_access import AwsAccess, AwsAccessError

                try:
                    AwsAccess().check_role(
                        AdapterEnvironment(
                            env_id=args.env_id,
                            role_arn=args.role_arn,
                            external_id=external_id,
                            region=args.region,
                        )
                    )
                except AwsAccessError as error:
                    print(
                        f"역할을 맡을 수 없어 등록하지 않았습니다: {error.error.code} - {error.error.message}"
                    )
                    return 1
                print("역할을 맡을 수 있고 계정이 일치합니다.")

            now, identifier = int(time.time()), str(uuid.uuid4())
            session.add(
                AwsEnvironment(
                    id=identifier,
                    workspace_id=workspace_id,
                    created_by=user.id,
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
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                print(
                    "등록하지 못했습니다. 같은 env_id나 External ID를 쓰는 환경이 이미 있을 수 있습니다."
                )
                return 1
            print(f"등록했습니다. 환경 ID: {identifier} (env_id {args.env_id})")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

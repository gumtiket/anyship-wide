"""실제 사용자 AWS 계정에서 AwsAlwaysOnAdapter를 시험하는 수동 시험(자동 테스트가 아니다).

서비스 서버에서 실행한다. 공용 기반(infra/user-account)이 이미 만들어져 있어야 한다.
smoke_onprem.py의 시험 앱과 확인 함수를 그대로 쓰고, AWS에서만 필요한 확인을 더한다.
check -> deploy -> 앱 계정으로 RDS 접속 -> 재배포 -> status -> rollback -> (없는 버전) -> destroy -> 앱 DB가 남았는지

    cd infra/user-account && ROLE_ARN=... EXTERNAL_ID=... ./tf.sh output -json > /tmp/outputs.json
    EXTERNAL_ID=... python scripts/smoke_aws_always_on.py --role-arn <역할 ARN> --outputs /tmp/outputs.json --staging --resolve

External ID는 명령줄이 아니라 환경변수로만 받는다(명령줄은 프로세스 목록과 히스토리에 남는다).
--resolve: 앱 주소를 DNS 대신 호스트 IP로 직접 연결한다(이 프로세스 안에서만). *.<환경ID>.aws 레코드를 만들기 전에 시험할 때 쓴다.
--keep: 마지막에 destroy하지 않는다. 시험이 끝나면 공용 기반을 destroy하는 것을 잊지 않는다(RDS 요금).
"""

import argparse
import json
import os
import socket
import sys
from pathlib import Path

import smoke_onprem as base
from smoke_onprem import APP, MISSING, V1, V2, expect, http, log, show, timed

from anyship_adapters import AwsEnvironment
from anyship_adapters.aws_access import AwsAccess
from anyship_adapters.aws_always_on import AwsAlwaysOnAdapter
from anyship_adapters.compose import POSTGRES_IMAGE
from anyship_adapters.rds_admin import MASTER_USER, db_name
from anyship_adapters.ssh import SshConnection, SshRunner

# 호스트에서 앱의 비밀 파일을 읽어 앱 계정으로 RDS에 접속한다. 값은 출력하지 않고 계정·DB 이름만 본다.
APP_CONNECT = (
    "set -a; . /opt/apps/{app}/app.env; set +a; exec docker run --rm -e DATABASE_URL {image} "
    'sh -c \'psql "$DATABASE_URL" -X -tA -c "select current_user || chr(124) || current_database()"\''
)
# 마스터 비밀번호는 표준입력 첫 줄로만 보낸다.
MASTER_QUERY = (
    'IFS= read -r PGPASSWORD || exit 1; export PGPASSWORD; exec docker run --rm -e PGPASSWORD "$1" '
    'psql -X -tA "host=$2 port=$3 user=$4 dbname=postgres sslmode=require connect_timeout=10" -c "$5"'
)


def environment(args, external_id: str) -> AwsEnvironment:
    out = {name: item["value"] for name, item in json.loads(Path(args.outputs).read_text()).items()}
    return AwsEnvironment(
        env_id=args.env_id,
        role_arn=args.role_arn,
        external_id=external_id,
        region=args.region,
        host=out["host_public_ip"],
        db_address=out["db_address"],
        db_port=out["db_port"],
        db_secret_arn=out["db_master_secret_arn"],
    )


def resolve_locally(name: str, ip: str) -> None:
    original = (
        socket.getaddrinfo
    )  # 이 이름만 호스트 IP로 연결한다. SNI와 Host 헤더는 이름 그대로 간다.
    socket.getaddrinfo = lambda host, *a, **k: original(ip if host == name else host, *a, **k)


def master_query(ssh, env, sql: str) -> str:
    password = AwsAccess().read_master_password(env)
    done = ssh.run(
        [
            "sh",
            "-c",
            MASTER_QUERY,
            "sh",
            POSTGRES_IMAGE,
            env.db_address,
            str(env.db_port),
            MASTER_USER,
            sql,
        ],
        input=password + "\n",
        timeout=120,
    )
    return done.stdout.strip() if done.ok else f"(실패 {done.returncode})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--outputs", required=True, help="`terraform output -json`을 저장한 파일")
    parser.add_argument("--env-id", default="test")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--key", default=str(Path.home() / ".ssh" / "onprem_deploy"))
    parser.add_argument("--domain", default="anyship.cloud")
    parser.add_argument(
        "--staging", action="store_true", help="Let's Encrypt staging 인증서면 TLS 검증을 끈다"
    )
    parser.add_argument(
        "--resolve", action="store_true", help="DNS 레코드 없이 호스트 IP로 직접 연결해 시험한다"
    )
    parser.add_argument(
        "--keep", action="store_true", help="마지막에 destroy하지 않고 시험 앱을 남긴다"
    )
    args = parser.parse_args()
    external_id = os.environ.get("EXTERNAL_ID")
    if not external_id:
        print("EXTERNAL_ID 환경변수를 지정하세요(명령줄 인자로는 받지 않습니다).")
        return 2

    env = environment(args, external_id)
    address = f"{APP}.{args.env_id}.aws.{args.domain}"
    url = f"https://{address}"
    if args.resolve:
        resolve_locally(address, env.host)
    ssh = SshRunner(SshConnection(env.host, Path(args.key)))
    adapter = AwsAlwaysOnAdapter(
        Path(args.key), base_domain=args.domain, verify_tls=not args.staging
    )
    ok = lambda: http(url + "/", args.staging)[0] == 200  # noqa: E731

    print("1) check")
    checked = timed(lambda: adapter.check(env, log))
    show(checked)
    if not checked.ok:
        return 1
    expect(
        "역할을 맡은 계정이 환경의 계정이다",
        checked.details.get("account_id") == args.role_arn.split(":")[4],
    )

    print(f"2) deploy {APP}:{V1}")
    base.build(V1)
    first = timed(lambda: adapter.deploy(env, base.SPEC, V1, {}, log, set_name="aws-always-on"))
    show(first)
    expect("배포가 성공한다", first.ok)
    if not first.ok:
        return 1
    expect("공개 주소가 200으로 응답한다", ok())
    expect("주소가 aws 도메인이다", first.url == url)

    print("3) 앱 계정으로 RDS에 접속(호스트에서)")
    connected = ssh.run(
        ["sh", "-c", APP_CONNECT.format(app=APP, image=POSTGRES_IMAGE)], timeout=120
    )
    expect(
        "앱 계정이 자기 DB에 SSL로 접속된다",
        connected.ok and connected.stdout.strip() == f"{db_name(APP)}|{db_name(APP)}",
    )

    print(f"4) 재배포 {APP}:{V2} (앱 DB 준비를 다시 실행하고 비밀을 재사용하는지)")
    base.build(V2)
    second = timed(lambda: adapter.deploy(env, base.SPEC, V2, {}, log, set_name="aws-always-on"))
    show(second)
    expect("재배포가 성공한다", second.ok)
    expect(
        "새로 만든 비밀이 없다(서버의 비밀을 재사용)",
        second.ok and second.details.get("generated") == [],
    )
    expect(
        "재배포 뒤에도 앱 계정이 접속된다",
        ssh.run(["sh", "-c", APP_CONNECT.format(app=APP, image=POSTGRES_IMAGE)], timeout=120).ok,
    )
    expect("재배포 뒤에도 200으로 응답한다", ok())

    print("5) status")
    expect("실행 중이고 최신 버전이다", adapter.status(env, APP).image_tag == V2)

    print(f"6) rollback {V1}")
    back = timed(lambda: adapter.rollback(env, APP, V1, log))
    show(back)
    expect("롤백이 성공한다", back.ok and back.image_tag == V1)
    expect("롤백 뒤에도 200으로 응답한다", ok())

    print(f"7) 서버에 없는 버전 {MISSING}으로 rollback (거부되어야 한다)")
    missing = adapter.rollback(env, APP, MISSING, log)
    expect("image_not_found로 거부된다", not missing.ok and missing.error.code == "image_not_found")

    if args.keep:
        print("8) --keep: destroy를 하지 않고 시험 앱을 남겼습니다.")
    else:
        print("8) destroy")
        gone = timed(lambda: adapter.destroy(env, APP, log))
        show(gone)
        expect("삭제가 성공한다", gone.ok and adapter.status(env, APP).state == "not_deployed")
        expect("삭제 뒤 주소가 200이 아니다", http(url + "/", args.staging)[0] != 200)
        expect("다시 삭제해도 성공한다", adapter.destroy(env, APP, log).ok)
        print("9) destroy는 앱 DB를 지우지 않는다")
        listed = master_query(
            ssh, env, f"select count(*) from pg_database where datname = '{db_name(APP)}'"
        )
        expect("앱 DB가 RDS에 남아 있다", listed == "1")

    failed = [label for label, passed in base.results if not passed]
    print(f"\n결과: {len(base.results) - len(failed)}/{len(base.results)} OK")
    for label in failed:
        print("  FAIL:", label)
    print(
        f"정리: 서비스 서버와 호스트에서 docker image rm {APP}:{V1} {APP}:{V2} / 앱 DB {db_name(APP)}는 기반을 destroy하면 함께 사라진다"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

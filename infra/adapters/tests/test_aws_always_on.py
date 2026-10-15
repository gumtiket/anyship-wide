import re
import shlex
import subprocess
from pathlib import Path

import pytest
from fakes import LifecycleServer, Log
from specs import GENERATED, make

from anyship_adapters import AdapterError, AwsEnvironment
from anyship_adapters.aws_access import AwsAccessError
from anyship_adapters.aws_always_on import AwsAlwaysOnAdapter
from anyship_adapters.compose_host import ComposeHost
from anyship_adapters.ssh import SshConnection, SshRunner

MASTER = "M4ster!Pass/word99"  # RDS가 만드는 비밀번호처럼 특수문자를 포함한다
SECRET_ARN = "arn:aws:secretsmanager:ap-northeast-2:223455088214:secret:rds!db-0a1b2c3d-AbCdEf"
DB_ADDRESS = "anyship-test-db.abc123.ap-northeast-2.rds.amazonaws.com"
FULL = dict(
    env_id="test",
    role_arn="arn:aws:iam::223455088214:role/deploy-service-role",
    external_id="ext-id-0123456789abcdef",
    host="203.0.113.5",
    db_address=DB_ADDRESS,
    db_secret_arn=SECRET_ARN,
)
ENV = AwsEnvironment(**FULL)
V1, V2 = "aaaaaaa", "bbbbbbb"
DIR = "/opt/apps/todo"
SET = "aws-always-on"


class AwsServer(LifecycleServer):
    """psql 호출은 파일 쓰기와 구분해서 기록하고, 답을 정할 수 있는 가짜 호스트."""

    def __init__(self, psql=None, **kwargs):
        super().__init__(**kwargs)
        self.psql = []  # (원격 명령, 표준입력)
        self._psql = psql or (lambda remote, stdin: (0, b"", b""))

    def __call__(self, cmd, **kwargs):
        remote = shlex.split(cmd[-1])
        if remote[:2] == ["sh", "-c"] and "psql" in remote[2]:
            stdin = kwargs["input"].decode()
            self.commands.append(remote)
            self.kwargs.append(kwargs)
            self.psql.append((remote, stdin))
            code, out, err = self._psql(remote, stdin)
            return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=err)
        return super().__call__(cmd, **kwargs)


class Access:
    """AWS를 부르지 않는 가짜. 호출 횟수를 센다."""

    def __init__(self, check=None, secret=MASTER):
        self.check_error, self.secret, self.checks, self.reads = check, secret, 0, 0

    def check_role(self, env):
        self.checks += 1
        if self.check_error:
            raise AwsAccessError(self.check_error)
        return "223455088214"

    def read_master_password(self, env):
        self.reads += 1
        if isinstance(self.secret, AdapterError):
            raise AwsAccessError(self.secret)
        return self.secret


class Health:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return (self.ok, 200 if self.ok else 502)


def setup(health=None, access=None, psql=None, **responses):
    srv = AwsServer(psql=psql, responses={tuple(k.split()): v for k, v in responses.items()})
    ssh = SshRunner(SshConnection("203.0.113.5", Path("/key")), runner=srv)
    check, access = health or Health(), access or Access()
    adapter = AwsAlwaysOnAdapter(
        Path("/key"),
        connect=lambda env: (ssh, ComposeHost(ssh, popen=srv.popen)),
        healthy=check,
        access=access,
    )
    return adapter, srv, check, access


def deploy(adapter, tag=V1, spec=GENERATED, secrets=None, log=None, env=ENV):
    return adapter.deploy(env, spec, tag, secrets or {}, log or Log(), set_name=SET)


def app_env(srv):
    return next(data for path, data in srv.files.items() if path.endswith("app.env")).decode()


def app_password(srv):
    return re.search(r"DATABASE_URL='postgresql://[^:]+:([A-Za-z0-9]+)@", app_env(srv)).group(1)


def argv(srv):
    return " ".join(" ".join(command) for command in srv.commands)


# --- check ------------------------------------------------------------------------------------
def test_check_passes_and_reports_the_account_and_host_in_five_steps():
    adapter, srv, _, access = setup()
    log = Log()
    result = adapter.check(ENV, log)
    assert result.ok and result.details == {"account_id": "223455088214", "host": "203.0.113.5"}
    assert [step for step, _, _ in log.steps()] == [1, 2, 3, 4, 5] and access.checks == 1


@pytest.mark.parametrize("field", ["host", "db_address", "db_secret_arn"])
def test_check_without_foundation_values_stops_before_touching_aws_or_the_host(field):
    adapter, srv, _, access = setup()
    result = adapter.check(AwsEnvironment(**{**FULL, field: None}), Log())
    assert (
        result.error.code == "foundation_missing"
        and field in result.error.message
        and result.error.hint
    )
    assert access.checks == 0 and srv.commands == []


def test_check_reports_a_role_error_without_connecting_to_the_host():
    error = AdapterError(code="access_denied", message="역할을 맡을 수 없습니다.")
    adapter, srv, _, _ = setup(access=Access(check=error))
    result = adapter.check(ENV, Log())
    assert result.error.code == "access_denied" and srv.commands == []


@pytest.mark.parametrize(
    "responses, code, retryable",
    [
        ({"true": (255, b"", b"Connection refused")}, "ssh_unreachable", True),
        (
            {"docker compose version": (1, b"", b"")},
            "docker_missing",
            True,
        ),  # 막 부팅한 호스트일 수 있다
        ({"docker ps": (0, b"", b"")}, "proxy_not_ready", True),
    ],
)
def test_check_reports_a_host_that_is_not_ready(responses, code, retryable):
    adapter, *_ = setup(**responses)
    result = adapter.check(ENV, Log())
    assert (result.error.code, result.error.retryable) == (code, retryable)


# --- deploy: 정상 경로 ---------------------------------------------------------------------------
def test_deploy_runs_every_step_and_returns_the_aws_address():
    adapter, srv, health, access = setup()
    log = Log()
    result = deploy(adapter, log=log)
    assert (
        result.ok and result.url == "https://todo.test.aws.anyship.cloud" and result.image_tag == V1
    )
    assert result.details["database"] == "app_todo"
    assert [s[:2] for s in log.steps()] == [(n, 8) for n in range(1, 9)]
    assert health.calls[0][0] == "https://todo.test.aws.anyship.cloud/healthz"
    assert srv.images == {f"todo:{V1}"} and "web" in srv.running


def test_deploy_creates_the_app_database_in_rds_and_points_the_app_at_it():
    adapter, srv, _, _ = setup()
    assert deploy(adapter).ok
    assert len(srv.psql) == 1 and DB_ADDRESS in srv.psql[0][0][-3]
    url = re.search(r"DATABASE_URL='([^']+)'", app_env(srv)).group(1)
    assert (
        url.startswith("postgresql://app_todo:")
        and f"@{DB_ADDRESS}:5432/app_todo?sslmode=require" in url
    )
    compose_yaml = next(
        data for path, data in srv.files.items() if path.endswith("compose.yaml")
    ).decode()
    assert "  db:" not in compose_yaml and "rds.amazonaws.com" not in compose_yaml


def test_deploy_runs_the_migration_inside_the_app_container():
    adapter, srv, _, _ = setup()
    assert deploy(adapter).ok
    assert any(
        c[:2] == ["docker", "compose"] and "run" in c and c[-1] == "python -m app.migrate"
        for c in srv.commands
    )


def test_deploy_says_the_dns_record_must_exist_instead_of_pretending_to_check_it():
    adapter, *_ = setup()
    log = Log()
    deploy(adapter, log=log)
    warnings = [e.message for e in log.events if e.level == "warn" and "DNS" in e.message]
    assert (
        len(warnings) == 1
        and "todo.test.aws.anyship.cloud" in warnings[0]
        and "203.0.113.5" in warnings[0]
    )
    assert all(name != "DNS 확인" for _, _, name in log.steps())


def test_when_the_service_manages_dns_the_log_does_not_claim_it_must_be_created_by_hand():
    # 서비스가 DNS를 자동으로 맞추는데 "미리 만들어 두어야 합니다(자동 생성 안 함)"이라고 알리면 사실과 달라 원인 파악을 흐린다
    srv = AwsServer(responses={})
    ssh = SshRunner(SshConnection("203.0.113.5", Path("/key")), runner=srv)
    adapter = AwsAlwaysOnAdapter(
        Path("/key"),
        connect=lambda env: (ssh, ComposeHost(ssh, popen=srv.popen)),
        healthy=Health(),
        access=Access(),
        dns_managed=True,
    )
    log = Log()
    assert deploy(adapter, log=log).ok
    assert not [e for e in log.events if e.level == "warn" and "DNS" in e.message]
    notes = [e.message for e in log.events if "DNS" in e.message]
    assert len(notes) == 1 and "자동으로" in notes[0] and "todo.test.aws.anyship.cloud" in notes[0]
    assert "미리 만들어" not in " ".join(e.message for e in log.events)


def test_redeploying_keeps_the_app_password_and_the_generated_secret_key():
    adapter, srv, _, _ = setup()
    deploy(adapter, V1)
    first_password, first_env = app_password(srv), app_env(srv)
    assert deploy(adapter, V2).ok
    assert app_password(srv) == first_password
    assert re.search(r"SECRET_KEY='[^']+'", app_env(srv)).group(0) == re.search(
        r"SECRET_KEY='[^']+'", first_env
    ).group(0)
    assert len(srv.psql) == 2  # DB 준비는 매번 실행하지만 같은 값으로 맞춘다


def test_a_spec_without_postgres_skips_the_database_and_never_reads_the_master_password():
    adapter, srv, _, access = setup()
    log = Log()
    result = deploy(adapter, spec=make(backing_services=[]), log=log)
    assert result.ok and result.details["database"] is None
    assert access.reads == 0 and srv.psql == [] and "DATABASE_URL" not in app_env(srv)
    assert [s[:2] for s in log.steps()] == [
        (n, 8) for n in range(1, 9)
    ]  # 건너뛴 단계도 번호는 유지


# --- deploy: 비밀 -----------------------------------------------------------------------------
def test_passwords_are_sent_only_on_standard_input_and_never_in_commands_logs_or_results():
    adapter, srv, _, _ = setup()
    log = Log()
    result = deploy(adapter, log=log)
    password = app_password(srv)
    assert MASTER not in argv(srv) and password not in argv(srv)
    assert srv.psql[0][1].split("\n", 1)[0] == MASTER and password in srv.psql[0][1]
    for text in (log.text(), result.model_dump_json()):
        assert MASTER not in text and password not in text
    # 앱 비밀번호는 비밀 파일(app.env)에만 있고, 다른 파일에는 없다.
    others = [data.decode() for path, data in srv.files.items() if not path.endswith("app.env")]
    assert all(password not in data and MASTER not in data for data in others)


def test_a_database_error_that_echoes_the_passwords_is_masked_everywhere():
    def leaky(remote, stdin):  # psql 오류가 실패한 문장(비밀번호 포함)을 그대로 보여 주는 경우
        return 1, b"", f"ERROR: syntax error at or near PASSWORD\n{stdin}".encode()

    adapter, srv, _, _ = setup(psql=leaky)
    log = Log()
    result = deploy(adapter, log=log)
    password = re.search(r"PASSWORD '([A-Za-z0-9]+)'", srv.psql[0][1]).group(1)
    assert not result.ok and result.error.code == "db_setup_failed" and result.error.retryable
    for text in (log.text(), result.model_dump_json()):
        assert MASTER not in text and password not in text
    assert "***" in result.details["stderr"]
    assert srv.images == set() and not any(
        path.endswith("compose.yaml") for path in srv.files
    )  # 이후 단계로 가지 않는다


def test_an_unreadable_master_secret_stops_the_deploy_before_any_database_command():
    error = AdapterError(
        code="secret_unreadable", message="DB 마스터 비밀번호를 읽지 못했습니다.", retryable=True
    )
    adapter, srv, _, _ = setup(access=Access(secret=error))
    result = deploy(adapter)
    assert result.error.code == "secret_unreadable" and srv.psql == [] and srv.images == set()


def test_user_supplied_secrets_are_masked_in_every_output():
    spec = make(env=GENERATED["env"] + [{"name": "STRIPE_KEY", "secret": True, "generate": False}])
    adapter, srv, _, _ = setup(
        **{"docker compose": (1, b"", b"error: STRIPE_KEY=sk-live-0123456789abcdef")}
    )
    log = Log()
    result = deploy(adapter, spec=spec, secrets={"STRIPE_KEY": "sk-live-0123456789abcdef"}, log=log)
    assert not result.ok
    assert "sk-live-0123456789abcdef" not in log.text() + result.model_dump_json()


def test_a_secret_that_ends_up_in_an_error_message_is_masked_before_the_result_leaves():
    # 오류 문구는 대개 고정 문장이지만, 하위 부품이 입력값을 문구에 섞어 돌려주는 경우를 대비한 안전망이다.
    secret = "sk-live-0123456789abcdef"
    spec = make(env=GENERATED["env"] + [{"name": "STRIPE_KEY", "secret": True, "generate": False}])
    error = AdapterError(
        code="secret_unreadable", message=f"failed near {secret}", hint=f"value {secret}"
    )
    adapter, *_ = setup(access=Access(secret=error))
    log = Log()
    result = deploy(adapter, spec=spec, secrets={"STRIPE_KEY": secret}, log=log)
    assert result.error.code == "secret_unreadable"
    assert secret not in result.model_dump_json() and secret not in log.text()


# --- deploy: 거부와 실패 ------------------------------------------------------------------------
def test_a_set_that_does_not_belong_to_this_adapter_is_refused():
    adapter, srv, _, access = setup()
    result = adapter.deploy(ENV, GENERATED, V1, {}, Log(), set_name="onprem")
    assert result.error.code == "set_not_supported" and srv.commands == [] and access.reads == 0


def test_deploy_without_foundation_values_does_nothing():
    adapter, srv, _, access = setup()
    result = deploy(adapter, env=AwsEnvironment(**{**FULL, "db_secret_arn": None}))
    assert result.error.code == "foundation_missing" and srv.commands == [] and access.reads == 0


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"tag": "not-a-sha"}, "invalid_image_tag"),
        ({"spec": make(app="a" * 60)}, "invalid_spec"),  # DB 이름(app_ + 앱 이름)이 63자를 넘는다
        (
            {
                "spec": make(
                    env=GENERATED["env"] + [{"name": "NEED", "secret": True, "generate": False}]
                )
            },
            "missing_secret",
        ),
    ],
)
def test_input_errors_are_found_before_connecting_to_anything(kwargs, code):
    adapter, srv, _, access = setup()
    result = deploy(adapter, **{"tag": V1, **kwargs})
    assert result.error.code == code and srv.commands == [] and access.reads == 0


@pytest.mark.parametrize(
    "responses, code",
    [
        ({"true": (255, b"", b"refused")}, "ssh_unreachable"),
        ({"docker load": (1, b"", b"no space left on device")}, "image_transfer_failed"),
        (
            {"docker compose --project-directory /opt/apps/todo up": (1, b"", b"boom")},
            "container_start_failed",
        ),
    ],
)
def test_deploy_failures_carry_a_code_and_stop_at_that_step(responses, code):
    adapter, srv, health, _ = setup(**responses)
    result = deploy(adapter)
    assert not result.ok and result.error.code == code and health.calls == []


def test_a_failed_migration_is_reported_and_the_health_check_is_not_reached():
    adapter, srv, health, _ = setup(
        **{"docker compose --project-directory /opt/apps/todo run": (1, b"", b"boom")}
    )
    result = deploy(adapter)
    assert result.error.code == "migration_failed" and health.calls == []


def test_a_failed_health_check_points_at_the_dns_record_too():
    adapter, *_ = setup(health=Health(ok=False))
    result = deploy(adapter)
    assert result.error.code == "healthcheck_failed" and result.error.retryable
    assert "DNS" in result.error.hint and "502" in result.error.message


# --- status, rollback, destroy --------------------------------------------------------------------
def test_status_rollback_and_destroy_use_the_aws_address_and_need_only_the_host():
    adapter, srv, health, _ = setup()
    host_only = AwsEnvironment(**{**FULL, "db_address": None, "db_secret_arn": None})
    assert deploy(adapter, V1).ok and deploy(adapter, V2).ok
    status = adapter.status(host_only, "todo")
    assert (
        status.state == "running"
        and status.url == "https://todo.test.aws.anyship.cloud"
        and status.image_tag == V2
    )
    back = adapter.rollback(host_only, "todo", V1, Log())
    assert back.ok and back.url == "https://todo.test.aws.anyship.cloud"
    assert adapter.destroy(host_only, "todo", Log()).ok
    assert srv.removed == [DIR] and adapter.status(host_only, "todo").state == "not_deployed"


def test_destroy_removes_the_app_but_leaves_its_database_in_rds():
    adapter, srv, _, _ = setup()
    deploy(adapter)
    psql_before = len(srv.psql)
    adapter.destroy(ENV, "todo", Log())
    assert len(srv.psql) == psql_before  # 삭제는 DB에 아무 명령도 보내지 않는다
    assert not re.search(r"drop (database|role)", argv(srv), re.IGNORECASE)


@pytest.mark.parametrize(
    "action",
    [
        lambda a, env: a.status(env, "todo"),
        lambda a, env: a.rollback(env, "todo", V1, Log()),
        lambda a, env: a.destroy(env, "todo", Log()),
    ],
)
def test_reading_and_cleaning_without_a_host_is_refused_before_connecting(action):
    adapter, srv, _, _ = setup()
    result = action(adapter, AwsEnvironment(**{**FULL, "host": None}))
    assert result.error.code == "foundation_missing" and srv.commands == []


def test_the_adapter_has_no_onprem_only_features():
    adapter, *_ = setup()
    assert not hasattr(adapter, "remove_environment_dns")

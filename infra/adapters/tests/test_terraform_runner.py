import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fakes import Log

from anyship_adapters import AdapterError, AwsEnvironment
from anyship_adapters import terraform_runner as tr
from anyship_adapters.aws_access import AwsAccessError, TemporaryCredentials
from anyship_adapters.terraform_runner import (
    MAX_LINE,
    TerraformError,
    TerraformRunner,
    apply_outputs,
)

BUCKET = "anyship-tfstate-223455088214-ap-northeast-2-2b9b6060"
ROLE = "arn:aws:iam::223455088214:role/deploy-service-role"
# 비밀 스캐너가 진짜 키로 오해하지 않도록 AKIA/ASIA로 시작하지 않는 가짜 값을 쓴다.
KEY, SECRET, TOKEN = "TESTKEYID0123456789", "test-secret-key", "test-token"
OUTPUTS = {
    "host_public_ip": {"value": "43.201.158.8"},
    "db_address": {"value": "anyship-test-db.abc.ap-northeast-2.rds.amazonaws.com"},
    "db_port": {"value": 5432},
    "db_master_secret_arn": {
        "value": "arn:aws:secretsmanager:ap-northeast-2:223455088214:secret:rds!db-abc-Xy1"
    },
    "vpc_id": {"value": "vpc-0123"},
}  # 기반 필드가 아닌 출력은 무시된다


def env(**override):
    values = dict(
        env_id="test", role_arn=ROLE, external_id="ext-id-0123456789abcdef", state_bucket=BUCKET
    )
    return AwsEnvironment(**{**values, **override})


@pytest.fixture(autouse=True)
def clean_locks():
    yield
    tr._ACTIVE.clear()  # 실패한 시험이 중복 실행 표시를 남겨 다른 시험에 번지지 않게 한다


@pytest.fixture
def module(tmp_path):
    directory = tmp_path / "module"
    directory.mkdir()
    (directory / "main.tf").write_text("")
    return directory


class Child:
    """가짜 Terraform 프로세스. hold를 주면 출력이 끝난 뒤에도 kill되기 전까지 끝나지 않는다."""

    def __init__(self, lines=(), code=0, hold=None):
        self._lines, self.code, self.hold, self.killed = list(lines), code, hold, False
        self.stdout = self._read()

    def _read(self):
        for line in self._lines:
            yield line + "\n"
        if self.hold is not None:
            self.hold.wait(5)

    def wait(self):
        return -9 if self.killed else self.code

    def kill(self):
        self.killed = True
        if self.hold is not None:
            self.hold.set()


def subcommand(args):
    return args[2]  # [terraform, -chdir=..., <하위 명령>, ...]


class Popen:
    def __init__(self, script=None):
        self.script = script or (lambda args: Child(["ok"]))
        self.calls, self.variable_files, self.children = [], [], []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        for arg in args:
            if arg.startswith(
                "-var-file="
            ):  # 실행이 끝나면 지워지므로, 시작하는 순간에 내용과 권한을 본다
                path = Path(arg.split("=", 1)[1])
                self.variable_files.append(
                    (path.read_text(encoding="utf-8"), path.stat().st_mode & 0o777)
                )
        child = self.script(list(args))
        self.children.append(child)
        return child

    def commands(self):
        return [call[0] for call in self.calls]


class Access:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def temporary_credentials(self, env, duration_seconds=3600):
        self.calls.append(duration_seconds)
        if self.error:
            raise AwsAccessError(self.error)
        return TemporaryCredentials(KEY, SECRET, TOKEN)


def runner(module, popen, access=None, **kwargs):
    return TerraformRunner(module, access=access or Access(), popen=popen, **kwargs)


def fail_on(sub, code, lines=("boom",)):
    """sub 하위 명령만 code로 실패하고 나머지는 성공하는 가짜."""
    return lambda args: Child(
        lines if subcommand(args) == sub else ["ok"], code if subcommand(args) == sub else 0
    )


# --- 출력 변환(순수) ---------------------------------------------------------------------------
def test_outputs_fill_the_foundation_fields_and_leave_the_original_untouched():
    original = env()
    filled = apply_outputs(original, json.dumps(OUTPUTS))
    assert (filled.host, filled.db_port) == ("43.201.158.8", 5432)
    assert filled.db_address.endswith(".rds.amazonaws.com") and filled.db_secret_arn.endswith(
        "rds!db-abc-Xy1"
    )
    assert filled.state_bucket == BUCKET and original.host is None  # 다른 필드는 유지, 원본은 불변


def test_an_empty_state_means_the_foundation_does_not_exist_yet():
    with pytest.raises(TerraformError) as caught:
        apply_outputs(env(), "{}")
    assert caught.value.error.code == "foundation_missing" and caught.value.error.hint


def without(name):
    return {key: value for key, value in OUTPUTS.items() if key != name}


@pytest.mark.parametrize(
    "text",
    [
        "oops",
        "[]",
        "null",
        "5",
        json.dumps(without("db_port")),
        json.dumps(without("host_public_ip")),
        json.dumps({**OUTPUTS, "db_port": {"type": "number"}}),  # value가 없는 항목
        json.dumps(
            {**OUTPUTS, "db_address": "anyship-test-db.abc.ap-northeast-2.rds.amazonaws.com"}
        ),  # 객체가 아님
    ],
)
def test_unreadable_or_incomplete_outputs_are_rejected(text):
    with pytest.raises(TerraformError) as caught:
        apply_outputs(env(), text)
    assert caught.value.error.code == "terraform_output_invalid"


@pytest.mark.parametrize(
    "field, value",
    [
        ("db_address", "evil.example.com"),
        ("host_public_ip", "bad host; rm -rf"),
        (
            "db_master_secret_arn",
            "arn:aws:secretsmanager:ap-northeast-2:111111111111:secret:rds!db-abc-Xy1",
        ),
        ("db_port", "abc"),
        ("db_port", 80),
    ],
)
def test_output_values_are_validated_again_and_never_echoed(field, value):
    with pytest.raises(TerraformError) as caught:
        apply_outputs(env(), json.dumps({**OUTPUTS, field: {"value": value}}))
    error = caught.value.error
    assert error.code == "terraform_output_invalid" and str(value) not in error.model_dump_json()
    assert {"host_public_ip": "host", "db_master_secret_arn": "db_secret_arn"}.get(
        field, field
    ) in error.message


# --- 입력 검사 ----------------------------------------------------------------------------------
def test_a_missing_state_bucket_stops_everything_before_credentials_or_processes(module):
    popen, access = Popen(), Access()
    with pytest.raises(TerraformError) as caught:
        runner(module, popen, access).plan(env(state_bucket=None), {}, Log())
    assert (
        caught.value.error.code == "foundation_missing"
        and "state_bucket" in caught.value.error.message
    )
    assert popen.calls == [] and access.calls == []


def test_a_folder_without_terraform_files_is_refused(tmp_path):
    popen, access = Popen(), Access()
    with pytest.raises(TerraformError) as caught:
        runner(tmp_path, popen, access).plan(env(), {}, Log())
    assert (
        caught.value.error.code == "terraform_module_missing"
        and popen.calls == []
        and access.calls == []
    )


@pytest.mark.parametrize(
    "variables", [{"Bad": 1}, {"1x": 1}, {"a-b": 1}, {"": 1}, {"x" * 65: 1}, {"ok": {1, 2}}]
)
def test_invalid_variables_are_refused_before_credentials_or_processes(module, variables):
    popen, access = Popen(), Access()
    with pytest.raises(TerraformError) as caught:
        runner(module, popen, access).apply(env(), variables, Log())
    assert (
        caught.value.error.code == "invalid_terraform_input"
        and popen.calls == []
        and access.calls == []
    )


@pytest.mark.parametrize("name", ["", "Bad", "a b", "a/b", "../x", "1a"])
def test_an_unsafe_state_name_is_refused(module, name):
    with pytest.raises(ValueError):
        TerraformRunner(module, state_name=name)


# --- 실행: 인자와 환경 ---------------------------------------------------------------------------
def test_init_runs_first_against_the_users_bucket_and_then_the_plan(module):
    popen = Popen(lambda args: Child(["ok"], 2 if subcommand(args) == "plan" else 0))
    assert runner(module, popen).plan(env(), {"region": "ap-northeast-2"}, Log()) is True
    init, plan = popen.commands()
    assert [subcommand(init), subcommand(plan)] == ["init", "plan"]
    assert (
        init[0] == "terraform" and init[1] == f"-chdir={module}"
    )  # -chdir은 하위 명령 앞에 와야 한다
    assert "-reconfigure" in init and "-input=false" in init and "-no-color" in init
    assert {
        "-backend-config=bucket=" + BUCKET,
        "-backend-config=region=ap-northeast-2",
        "-backend-config=key=test/foundation.tfstate",
    } <= set(init)
    assert "-detailed-exitcode" in plan and "-auto-approve" not in plan
    assert (
        "-input=false" in plan
        and "-no-color" in plan
        and any(a.startswith("-var-file=") for a in plan)
    )


def test_plan_reports_whether_anything_would_change(module):
    assert runner(module, Popen(lambda args: Child([], 0))).plan(env(), {}, Log()) is False
    assert (
        runner(module, Popen(lambda args: Child([], 2 if subcommand(args) == "plan" else 0))).plan(
            env(), {}, Log()
        )
        is True
    )


def test_apply_approves_automatically_and_does_not_use_the_plan_flags(module):
    popen = Popen()
    runner(module, popen).apply(env(), {}, Log())
    apply = popen.commands()[1]
    assert (
        subcommand(apply) == "apply"
        and "-auto-approve" in apply
        and "-detailed-exitcode" not in apply
    )


def test_the_backend_region_comes_from_the_bucket_name_and_the_key_from_the_environment(module):
    popen = Popen()
    other = env(env_id="demo-2", state_bucket="anyship-tfstate-223455088214-us-east-1-0123abcd")
    runner(module, popen, state_name="foundation").plan(other, {}, Log())
    init = popen.commands()[0]
    assert (
        "-backend-config=region=us-east-1" in init
        and "-backend-config=key=demo-2/foundation.tfstate" in init
    )


def test_variables_are_written_to_a_private_file_that_is_gone_afterwards(module):
    popen = Popen()
    runner(module, popen).apply(env(), {"region": "ap-northeast-2", "host_volume_size": 30}, Log())
    content, mode = popen.variable_files[0]
    assert json.loads(content) == {"region": "ap-northeast-2", "host_volume_size": 30}
    if os.name != "nt":
        assert mode == 0o600
    path = Path(next(a for a in popen.commands()[1] if a.startswith("-var-file=")).split("=", 1)[1])
    assert not path.exists()


def test_each_run_gets_its_own_data_dir_outside_the_module_and_removes_it(module):
    popen = Popen()
    runner(module, popen).plan(env(), {}, Log())
    runner(module, popen).plan(env(), {}, Log())
    dirs = [Path(kwargs["env"]["TF_DATA_DIR"]) for _, kwargs in popen.calls]
    assert (
        dirs[0] == dirs[1] and dirs[2] == dirs[3] and dirs[0] != dirs[2]
    )  # 실행 안에서는 같고 실행마다 다르다
    assert all(
        module not in d.parents and not d.parent.exists() for d in dirs
    )  # 모듈 밖이고, 끝나면 지워진다


def test_the_temp_files_are_removed_even_when_terraform_fails(module):
    popen = Popen(fail_on("plan", 1))
    with pytest.raises(TerraformError):
        runner(module, popen).plan(env(), {}, Log())
    assert not Path(popen.calls[0][1]["env"]["TF_DATA_DIR"]).parent.exists()


def test_the_process_gets_only_the_assumed_credentials_and_safe_settings(module, monkeypatch):
    for name, value in {
        "AWS_ACCESS_KEY_ID": "service-key",
        "AWS_SECRET_ACCESS_KEY": "service-secret",
        "AWS_PROFILE": "service-profile",
        "AWS_SESSION_TOKEN": "service-token",
        "SERVICE_ONLY_SECRET": "service-only",
    }.items():
        monkeypatch.setenv(name, value)
    popen, access = Popen(), Access()
    runner(module, popen, access).plan(env(), {}, Log())
    process = popen.calls[0][1]["env"]
    assert (
        process["AWS_ACCESS_KEY_ID"],
        process["AWS_SECRET_ACCESS_KEY"],
        process["AWS_SESSION_TOKEN"],
    ) == (KEY, SECRET, TOKEN)  # 서비스 서버의 값이 아니라 AssumeRole한 값
    assert "AWS_PROFILE" not in process and "SERVICE_ONLY_SECRET" not in process
    assert process["AWS_EC2_METADATA_DISABLED"] == "true" and process["TF_INPUT"] == "0"
    assert (
        process["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
        and "TF_PLUGIN_CACHE_DIR" not in process
    )
    assert access.calls == [3600]  # 한 번만, 한 시간짜리로 받는다
    assert (
        popen.calls[0][1]["stdin"] == subprocess.DEVNULL
    )  # 비밀번호 같은 입력을 기다리며 멈추지 않는다


def test_a_plugin_cache_directory_is_passed_when_given(module, tmp_path):
    popen = Popen()
    runner(module, popen, plugin_cache_dir=tmp_path / "cache").plan(env(), {}, Log())
    assert popen.calls[0][1]["env"]["TF_PLUGIN_CACHE_DIR"] == str(tmp_path / "cache")


# --- 실행: 비밀과 로그 ---------------------------------------------------------------------------
ECHOES = (
    f"export AWS_ACCESS_KEY_ID={KEY}",
    f"AWS_SECRET_ACCESS_KEY={SECRET}",
    f"token {TOKEN} expired",
)


def test_credentials_are_in_the_environment_only_never_in_arguments_files_or_logs(module):
    popen, log = Popen(lambda args: Child(ECHOES)), Log()
    runner(module, popen).apply(env(), {"region": "ap-northeast-2"}, log)
    assert all(
        v not in arg for args in popen.commands() for arg in args for v in (KEY, SECRET, TOKEN)
    )
    assert all(
        v not in content for content, _ in popen.variable_files for v in (KEY, SECRET, TOKEN)
    )
    assert all(v not in log.text() for v in (KEY, SECRET, TOKEN)) and "***" in log.text()


def test_an_error_that_echoes_credentials_is_masked_in_the_message_and_the_tail(module):
    popen = Popen(fail_on("apply", 1, ECHOES))
    log = Log()
    with pytest.raises(TerraformError) as caught:
        runner(module, popen).apply(env(), {}, log)
    error = caught.value
    assert (
        all(
            v not in error.tail and v not in error.error.model_dump_json() and v not in log.text()
            for v in (KEY, SECRET, TOKEN)
        )
        and "***" in error.tail
    )


def test_progress_lines_arrive_with_step_and_name_and_blank_lines_are_dropped(module):
    log = Log()
    runner(module, Popen(lambda args: Child(["", "Initializing", "  ", "Done"]))).plan(
        env(), {}, log
    )
    messages = [(e.step, e.total, e.name, e.message) for e in log.events]
    assert messages[0] == (1, 2, "terraform init", "terraform init 실행")
    assert (1, 2, "terraform init", "Initializing") in messages and (
        2,
        2,
        "terraform plan",
        "Done",
    ) in messages
    assert not any(m[3].strip() == "" for m in messages)


def test_a_very_long_line_is_truncated(module):
    log = Log()
    runner(module, Popen(lambda args: Child(["x" * 5000]))).plan(env(), {}, log)
    assert max(len(e.message) for e in log.events) <= MAX_LINE


# --- 실행: 실패 ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sub, run, code, retryable",
    [
        ("init", lambda r, e, log: r.plan(e, {}, log), "terraform_init_failed", False),
        ("plan", lambda r, e, log: r.plan(e, {}, log), "terraform_plan_failed", True),
        ("apply", lambda r, e, log: r.apply(e, {}, log), "terraform_apply_failed", True),
    ],
)
def test_a_failing_command_becomes_a_coded_error_with_the_tail_of_its_output(
    module, sub, run, code, retryable
):
    popen = Popen(fail_on(sub, 1, ("line one", "line two")))
    with pytest.raises(TerraformError) as caught:
        run(runner(module, popen), env(), Log())
    error = caught.value
    assert (error.error.code, error.error.retryable) == (
        code,
        retryable,
    ) and "line two" in error.tail


def test_a_failed_init_means_the_real_command_never_starts(module):
    popen = Popen(fail_on("init", 1))
    with pytest.raises(TerraformError):
        runner(module, popen).apply(env(), {}, Log())
    assert [subcommand(c) for c in popen.commands()] == ["init"]


def test_plan_exit_code_three_is_a_failure_not_a_change(module):
    with pytest.raises(TerraformError) as caught:
        runner(module, Popen(fail_on("plan", 3))).plan(env(), {}, Log())
    assert caught.value.error.code == "terraform_plan_failed"


def test_a_state_lock_conflict_is_reported_as_retryable_and_distinct(module):
    popen = Popen(fail_on("apply", 1, ("Error: Error acquiring the state lock", "Lock Info: ...")))
    with pytest.raises(TerraformError) as caught:
        runner(module, popen).apply(env(), {}, Log())
    assert (
        caught.value.error.code == "terraform_locked"
        and caught.value.error.retryable
        and caught.value.error.hint
    )


def test_a_missing_terraform_binary_is_reported(module):
    def missing(args, **kwargs):
        raise FileNotFoundError("terraform")

    with pytest.raises(TerraformError) as caught:
        runner(module, missing).plan(env(), {}, Log())
    assert caught.value.error.code == "terraform_not_found"


def test_a_credential_failure_is_passed_through_and_no_process_starts(module):
    error = AdapterError(code="access_denied", message="역할을 맡을 수 없습니다.")
    popen = Popen()
    with pytest.raises(TerraformError) as caught:
        runner(module, popen, Access(error)).plan(env(), {}, Log())
    assert caught.value.error.code == "access_denied" and popen.calls == []


def test_a_run_that_takes_too_long_is_killed_and_reported(module):
    hold = threading.Event()
    children = []

    def script(args):
        child = Child(["working"], hold=hold) if subcommand(args) != "init" else Child([])
        children.append(child)
        return child

    with pytest.raises(TerraformError) as caught:
        runner(module, Popen(script), timeout=0.1).apply(env(), {}, Log())
    assert caught.value.error.code == "terraform_timeout" and caught.value.error.retryable
    assert children[-1].killed
    runner(module, Popen(), timeout=5).plan(
        env(), {}, Log()
    )  # 시간 초과 뒤에도 같은 환경을 다시 쓸 수 있다


# --- 동시 실행과 취소 ---------------------------------------------------------------------------
def test_a_second_run_on_the_same_state_is_refused_while_the_first_is_running(module):
    release, started = threading.Event(), threading.Event()

    def slow(args):
        if subcommand(args) != "init":
            started.set()
            return Child(["working"], hold=release)
        return Child([])

    first = threading.Thread(target=lambda: runner(module, Popen(slow)).apply(env(), {}, Log()))
    first.start()
    try:
        assert started.wait(5)
        with pytest.raises(TerraformError) as caught:
            runner(module, Popen()).apply(env(), {}, Log())
        assert caught.value.error.code == "terraform_locked" and caught.value.error.retryable
        runner(module, Popen()).plan(env(env_id="other"), {}, Log())  # 다른 환경은 막지 않는다
        runner(module, Popen(), state_name="other").plan(
            env(), {}, Log()
        )  # 같은 환경의 다른 state도 막지 않는다
    finally:
        release.set()
        first.join(5)
    runner(module, Popen()).plan(env(), {}, Log())  # 끝난 뒤에는 다시 실행할 수 있다


def test_a_failed_run_releases_the_lock(module):
    with pytest.raises(TerraformError):
        runner(module, Popen(fail_on("apply", 1))).apply(env(), {}, Log())
    runner(module, Popen()).apply(env(), {}, Log())


def test_cancelling_through_the_log_callback_kills_terraform_and_releases_the_lock(module):
    popen = Popen(lambda args: Child(["a", "b"]) if subcommand(args) != "init" else Child([]))

    def cancel(event):
        if event.message == "a":
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError):
        runner(module, popen).apply(env(), {}, cancel)
    assert popen.children[-1].killed
    runner(module, Popen()).apply(env(), {}, Log())


# --- 기반 출력 읽기 -----------------------------------------------------------------------------
def output_script(lines=None, code=0):
    body = json.dumps(OUTPUTS, indent=2).splitlines() if lines is None else lines
    return lambda args: Child(body, code) if subcommand(args) == "output" else Child([])


def test_read_foundation_returns_the_environment_with_the_foundation_filled_in(module):
    popen, log = Popen(output_script()), Log()
    filled = runner(module, popen).read_foundation(env(), log)
    assert (filled.host, filled.db_port) == ("43.201.158.8", 5432)
    command = popen.commands()[1]
    assert subcommand(command) == "output" and "-json" in command and "-no-color" in command
    assert not any(
        a.startswith(("-var-file", "-input")) for a in command
    )  # output은 이 옵션을 받지 않는다
    assert popen.variable_files == []


def test_the_raw_output_json_is_data_for_the_caller_and_stays_out_of_the_log(module):
    log = Log()
    runner(module, Popen(output_script())).read_foundation(env(), log)
    assert "43.201.158.8" not in log.text() and "rds.amazonaws.com" not in log.text()


def test_reading_an_empty_state_says_the_foundation_is_missing(module):
    with pytest.raises(TerraformError) as caught:
        runner(module, Popen(output_script(["{}"]))).read_foundation(env(), Log())
    assert caught.value.error.code == "foundation_missing"


def test_a_failing_output_command_is_reported_with_its_masked_tail(module):
    with pytest.raises(TerraformError) as caught:
        runner(module, Popen(output_script(list(ECHOES), 1))).read_foundation(env(), Log())
    error = caught.value
    assert error.error.code == "terraform_output_failed" and all(
        v not in error.tail for v in (KEY, SECRET, TOKEN)
    )


# --- 진짜 하위 프로세스 -------------------------------------------------------------------------
FAKE_TERRAFORM = """import os, sys
args = sys.argv[1:]
print("sub:", args[1])
print("secret in env:", os.environ["AWS_SECRET_ACCESS_KEY"])
print("metadata:", os.environ.get("AWS_EC2_METADATA_DISABLED"))
print("service-only:", os.environ.get("SERVICE_ONLY_SECRET"))
print("stdin:", repr(sys.stdin.read()))
sys.exit(2 if args[1] == "plan" else 0)
"""


def test_it_works_with_a_real_subprocess(module, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_ONLY_SECRET", "service-only-value")
    script = tmp_path / "fake_terraform.py"
    script.write_text(FAKE_TERRAFORM)

    def real(args, **kwargs):
        if os.name == "nt":  # Windows의 파이썬은 SYSTEMROOT 없이는 시작하지 못한다
            kwargs["env"] = {**kwargs["env"], "SYSTEMROOT": os.environ["SYSTEMROOT"]}
        return subprocess.Popen([sys.executable, str(script)] + args[1:], **kwargs)

    log = Log()
    assert runner(module, real).plan(env(), {}, log) is True
    text = log.text()
    assert "sub: init" in text and "sub: plan" in text
    assert (
        SECRET not in text and "secret in env: ***" in text
    )  # 프로세스는 값을 받았고, 로그에서는 가려진다
    assert "metadata: true" in text and "service-only: None" in text and "stdin: ''" in text

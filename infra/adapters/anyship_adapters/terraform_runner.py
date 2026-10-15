"""사용자 AWS 계정에서 Terraform을 실행한다(공용 기반 생성 등). `infra/user-account/tf.sh`를 코드로 옮긴 것이다.

지키는 것:
  * 자격 증명은 AssumeRole한 임시 자격 증명을 **하위 프로세스의 환경변수로만** 준다(파일, 명령줄, 로그에 없음).
    서비스 서버의 AWS 환경은 넘기지 않고, 인스턴스 메타데이터도 쓰지 못하게 해서 폴백을 막는다.
  * 실행마다 임시 `TF_DATA_DIR`을 쓴다. 모듈 폴더(`.terraform`)를 건드리지 않으므로 동시 실행이 섞이지 않고,
    모듈이 읽는 형제 폴더(`../onprem-vm/scripts` 등)는 제자리에서 그대로 읽힌다.
  * state는 사용자 계정의 버킷(`env.state_bucket`)에 `<환경ID>/<이름>.tfstate`로 둔다.
  * Terraform의 출력 줄은 비밀을 가려서 실시간으로 `log`에 보낸다.

실패는 TerraformError(AdapterError 포함)로 던진다. 호출하는 어댑터가 결과의 오류로 바꾼다.
"""

import json
import os
import re
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .aws_access import AwsAccess, AwsAccessError, TemporaryCredentials
from .base import LogFn
from .models import AdapterError, AwsEnvironment, LogEvent
from .redact import redact_text
from .ssh import safe_env

_BUCKET = re.compile(r"^anyship-tfstate-[0-9]{12}-([a-z]{2}(?:-[a-z]+)+-[0-9])-[0-9a-f]{8}$")
_VAR_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STATE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
MAX_LINE = 1000  # 한 줄이 아주 길어도 로그가 불어나지 않게 자른다
TAIL_LINES = 15  # 실패했을 때 오류에 붙여 줄 마지막 출력 줄 수


class TerraformError(Exception):
    """실행할 수 없거나 Terraform이 실패했을 때 던진다. tail은 비밀을 가린 마지막 출력(결과의 details용)이다."""

    def __init__(self, error: AdapterError, tail: str = ""):
        super().__init__(error.message)
        self.error, self.tail = error, tail


def _fail(
    code: str, message: str, hint: str | None = None, retryable: bool = False, tail: str = ""
) -> TerraformError:
    return TerraformError(
        AdapterError(code=code, message=message, hint=hint, retryable=retryable), tail
    )


# 공용 기반 출력 이름 -> AwsEnvironment 필드. infra/user-account/outputs.tf와 같아야 한다.
_OUTPUTS = {
    "host_public_ip": "host",
    "db_address": "db_address",
    "db_port": "db_port",
    "db_master_secret_arn": "db_secret_arn",
}
_ACTIVE: set[tuple[str, str, str]] = set()  # 지금 실행 중인 (버킷, 환경 ID, state 이름)
_GUARD = threading.Lock()


def apply_outputs(env: AwsEnvironment, text: str) -> AwsEnvironment:
    """`terraform output -json`의 내용을 환경 정보의 기반 필드에 반영한 새 환경을 돌려준다(값은 다시 검증한다)."""
    try:
        data = json.loads(text)
    except ValueError:
        raise _fail("terraform_output_invalid", "Terraform 출력을 읽지 못했습니다.") from None
    if data == {}:  # 기반이 아직 없거나 모두 지워진 state
        raise _fail(
            "foundation_missing",
            "공용 기반이 아직 만들어지지 않았습니다.",
            hint="공용 기반을 먼저 만들어 주세요.",
        )
    missing = [
        name
        for name in _OUTPUTS
        if not (
            isinstance(data, dict) and isinstance(data.get(name), dict) and "value" in data[name]
        )
    ]
    if missing:
        raise _fail(
            "terraform_output_invalid",
            "Terraform 출력에 필요한 항목이 없습니다: " + ", ".join(missing),
        )
    try:  # 값은 로그나 오류에 싣지 않고, 틀린 필드 이름만 알린다
        return AwsEnvironment.model_validate(
            {**env.model_dump(), **{field: data[name]["value"] for name, field in _OUTPUTS.items()}}
        )
    except ValidationError as exc:
        fields = set()
        for err in exc.errors():
            if err["loc"]:
                fields.add(str(err["loc"][0]))
            else:  # 모델 수준 검증(계정 일치 등)은 위치가 없고, 우리가 쓴 메시지에 필드 이름이 들어 있다
                fields.update(
                    name for name in ("state_bucket", "db_secret_arn") if name in err["msg"]
                )
        raise _fail(
            "terraform_output_invalid",
            "Terraform 출력 값의 형식이 올바르지 않습니다: "
            + ", ".join(sorted(fields) or ["환경 정보"]),
        ) from None


class TerraformRunner:
    def __init__(
        self,
        module_dir: Path,
        *,
        access: AwsAccess | None = None,
        binary: str = "terraform",
        state_name: str = "foundation",
        plugin_cache_dir: Path | None = None,
        timeout: float = 2700,
        popen=subprocess.Popen,
    ):
        if not _STATE_NAME.match(state_name):
            raise ValueError("invalid state name")
        self._dir, self._access, self._binary = Path(module_dir), access or AwsAccess(), binary
        self._state_name, self._cache, self._timeout = state_name, plugin_cache_dir, timeout
        self._popen = popen  # 시험에서는 가짜 프로세스로 바꿔 끼운다

    def plan(self, env: AwsEnvironment, variables: Mapping[str, Any], log: LogFn) -> bool:
        """변경이 있으면 True. 아무것도 만들거나 바꾸지 않는다."""
        return (
            self._run(
                env, variables, log, ["plan", "-detailed-exitcode"], (0, 2), "terraform_plan_failed"
            )
            == 2
        )

    def apply(self, env: AwsEnvironment, variables: Mapping[str, Any], log: LogFn) -> None:
        self._run(env, variables, log, ["apply", "-auto-approve"], (0,), "terraform_apply_failed")

    def read_foundation(self, env: AwsEnvironment, log: LogFn) -> AwsEnvironment:
        """state의 출력을 읽어, 기반 필드(host, db_address, db_port, db_secret_arn)가 채워진 새 환경을 돌려준다."""
        captured: list[str] = []
        self._run(env, None, log, ["output", "-json"], (0,), "terraform_output_failed", captured)
        return apply_outputs(env, "".join(captured))

    # -- 내부 ---------------------------------------------------------------------------------
    @contextmanager
    def _exclusive(self, env: AwsEnvironment) -> Iterator[None]:
        # 같은 state를 동시에 건드리지 않게 한다. S3 잠금과 별개로, 이 프로세스 안에서 먼저 막아 이유를 분명히 알린다.
        key = (env.state_bucket or "", env.env_id, self._state_name)
        with _GUARD:
            if key in _ACTIVE:
                raise _fail(
                    "terraform_locked",
                    "같은 환경에서 다른 Terraform 작업이 실행 중입니다.",
                    hint="그 작업이 끝난 뒤 다시 시도해 주세요.",
                    retryable=True,
                )
            _ACTIVE.add(key)
        try:
            yield
        finally:
            with _GUARD:
                _ACTIVE.discard(key)

    def _check(self, env: AwsEnvironment, variables: Mapping[str, Any]) -> str:
        if env.state_bucket is None:
            raise _fail(
                "foundation_missing",
                "사용자 계정의 공용 기반 정보가 없습니다: state_bucket",
                hint="온보딩 스택 출력의 StateBucketName을 환경 정보에 넣어 주세요.",
            )
        if not any(self._dir.glob("*.tf")):
            raise _fail("terraform_module_missing", "실행할 Terraform 모듈을 찾지 못했습니다.")
        bad = [name for name in variables if not isinstance(name, str) or not _VAR_NAME.match(name)]
        if bad:
            raise _fail("invalid_terraform_input", "Terraform 변수 이름 형식이 올바르지 않습니다.")
        try:
            json.dumps(dict(variables))
        except (TypeError, ValueError):
            raise _fail(
                "invalid_terraform_input", "Terraform 변수 값을 JSON으로 만들 수 없습니다."
            ) from None
        return _BUCKET.match(env.state_bucket).group(1)  # 버킷이 있는 리전(이름에 들어 있다)

    def _environment(self, credentials: TemporaryCredentials, work: Path) -> dict[str, str]:
        process = {
            **safe_env(),
            **credentials.environ(),
            "TF_DATA_DIR": str(work / "data"),
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
            "AWS_EC2_METADATA_DISABLED": "true",  # 자격 증명이 잘못돼도 인스턴스 역할로 폴백하지 않는다
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_CONFIG_FILE": os.devnull,
        }
        if self._cache:
            process["TF_PLUGIN_CACHE_DIR"] = str(self._cache)
        return process

    def _run(self, env, variables, log, command, ok_codes, failure_code, capture=None) -> int:
        bucket_region = self._check(env, variables or {})
        with self._exclusive(env):
            return self._run_exclusive(
                env, variables, log, command, ok_codes, failure_code, capture, bucket_region
            )

    def _run_exclusive(
        self, env, variables, log, command, ok_codes, failure_code, capture, bucket_region
    ) -> int:
        try:
            credentials = self._access.temporary_credentials(env)
        except AwsAccessError as exc:
            raise TerraformError(exc.error) from None
        secrets = credentials.secret_values()
        with tempfile.TemporaryDirectory(prefix="anyship-tf-") as directory:
            work = Path(directory)
            flags = ["-no-color"]  # output은 -input과 -var-file을 받지 않는다
            if variables is not None:
                variable_file = work / "variables.json"
                variable_file.write_text(json.dumps(dict(variables)), encoding="utf-8")
                variable_file.chmod(0o600)
                flags = ["-input=false", "-no-color", f"-var-file={variable_file}"]
            process, base = (
                self._environment(credentials, work),
                [self._binary, f"-chdir={self._dir}"],
            )
            backend = [
                f"-backend-config=bucket={env.state_bucket}",
                f"-backend-config=region={bucket_region}",
                f"-backend-config=key={env.env_id}/{self._state_name}.tfstate",
            ]
            self._execute(
                base + ["init", "-input=false", "-no-color", "-reconfigure", *backend],
                process,
                secrets,
                log,
                1,
                "terraform init",
                (0,),
                "terraform_init_failed",
            )
            return self._execute(
                base + [*command, *flags],
                process,
                secrets,
                log,
                2,
                f"terraform {command[0]}",
                ok_codes,
                failure_code,
                capture,
            )

    def _execute(
        self, args, process, secrets, log, step, label, ok_codes, failure_code, capture=None
    ) -> int:
        log(LogEvent(step=step, total=2, name=label, message=f"{label} 실행"))
        try:
            child = self._popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=process,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            raise _fail(
                "terraform_not_found", "서비스 서버에서 terraform을 찾을 수 없습니다."
            ) from None
        timed_out, tail = [], deque(maxlen=TAIL_LINES)

        def expire():
            timed_out.append(True)
            child.kill()

        timer = threading.Timer(self._timeout, expire)
        timer.start()
        try:
            for raw in child.stdout:
                if capture is not None:  # 출력 결과는 호출한 쪽이 읽는 데이터라 로그에 싣지 않는다
                    capture.append(raw)
                    continue
                line = redact_text(raw.rstrip()[:MAX_LINE], secrets)
                if line:
                    tail.append(line)
                    log(LogEvent(step=step, total=2, name=label, message=line))
            code = child.wait()
        except (
            BaseException
        ):  # 로그 함수가 예외를 던지거나 작업이 취소되면 Terraform을 남겨 두지 않는다
            child.kill()
            child.wait()
            raise
        finally:
            timer.cancel()
        text = (
            "\n".join(tail) if capture is None else redact_text("".join(capture)[-2000:], secrets)
        )
        if timed_out:
            raise _fail(
                "terraform_timeout",
                "Terraform이 제한 시간 안에 끝나지 않아 중단했습니다.",
                hint="다시 실행하면 만들어진 것은 이어서 진행됩니다.",
                retryable=True,
                tail=text,
            )
        if code in ok_codes:
            return code
        if "Error acquiring the state lock" in text:
            raise _fail(
                "terraform_locked",
                "같은 환경에서 다른 Terraform 작업이 실행 중입니다.",
                hint="그 작업이 끝난 뒤 다시 시도해 주세요.",
                retryable=True,
                tail=text,
            )
        raise _fail(
            failure_code,
            f"{label}이(가) 실패했습니다.",
            retryable=failure_code != "terraform_init_failed",
            tail=text,
        )

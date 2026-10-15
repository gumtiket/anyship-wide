"""APP_AWS_VERIFY 설정: 값 검증과, main.py가 STSAdapter를 켜고 끄는 방식.

DB나 가짜 GitHub 없이 돈다. main.py는 불러오는 순간 앱을 만들고 boto3를 어떻게 불러오는지가 시험 대상이라,
같은 pytest 프로세스(다른 테스트가 이미 boto3를 불러옴)가 아니라 별도 프로세스에서 확인한다.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.config import Settings

SERVICE_DIR = Path(__file__).resolve().parents[1]
TEMPLATE = "https://example.s3.ap-northeast-2.amazonaws.com/onboarding.yaml?versionId=one+two&x=1"
SERVICE_ROLE = "arn:aws:iam::999999999999:role/service-server"
PROBE = """
import json, sys
from fastapi.testclient import TestClient
import app.main as main
config = TestClient(main.app).get("/api/config").json()
print(json.dumps({"adapter": type(main.aws_adapter).__name__,
                  "verification": config["aws_verification_available"],
                  "sts_module_loaded": "app.aws_sts_adapter" in sys.modules}))
"""


def settings(**values):
    return Settings(token_key=Fernet.generate_key().decode(), **values)


# --- 설정 값 ---------------------------------------------------------------------------------
def test_verification_is_off_by_default():
    assert settings().aws_verify == ""


def test_sts_is_accepted():
    assert settings(aws_verify="sts").aws_verify == "sts"


@pytest.mark.parametrize("value", ["STS", "yes", "mock", "on", " sts", "sts "])
def test_any_other_value_is_refused(value):
    with pytest.raises(ValueError, match="APP_AWS_VERIFY"):
        settings(aws_verify=value)


@pytest.mark.parametrize(
    "raw, expected", [("", ""), ("sts", "sts"), (" STS ", "sts"), ("Sts", "sts")]
)
def test_the_environment_variable_is_trimmed_and_lowercased(monkeypatch, raw, expected):
    monkeypatch.setenv("APP_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_AWS_VERIFY", raw)
    assert Settings.from_env().aws_verify == expected


def test_a_missing_environment_variable_means_off(monkeypatch):
    monkeypatch.setenv("APP_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("APP_AWS_VERIFY", raising=False)
    assert Settings.from_env().aws_verify == ""


def test_a_misspelled_environment_variable_stops_the_service_from_starting(monkeypatch):
    monkeypatch.setenv("APP_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_AWS_VERIFY", "bogus")
    with pytest.raises(ValueError, match="APP_AWS_VERIFY"):
        Settings.from_env()


# --- main.py가 어댑터를 연결하는 방식(별도 프로세스) ------------------------------------------------
def start(tmp_path, verify):
    # 개발자 PC의 APP_* 설정이 섞이지 않게 비우고, 시험에 필요한 값만 넣는다.
    env = {key: value for key, value in os.environ.items() if not key.startswith("APP_")}
    env.update(
        APP_TOKEN_KEY=Fernet.generate_key().decode(),
        APP_DATABASE_URL=f"sqlite:///{tmp_path / 'main.db'}",
        APP_AWS_TEMPLATE_URL=TEMPLATE,
        APP_AWS_SERVICE_ROLE_ARN=SERVICE_ROLE,
        APP_AWS_REGIONS="ap-northeast-2",
        APP_AWS_VERIFY=verify,
        PYTHONWARNINGS="ignore",
    )
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=SERVICE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_entrypoint_leaves_verification_off_and_does_not_load_boto3_by_default(tmp_path):
    done = start(tmp_path, "")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {
        "adapter": "NoneType",
        "verification": False,
        "sts_module_loaded": False,
    }


def test_the_entrypoint_connects_the_sts_adapter_only_when_asked(tmp_path):
    done = start(tmp_path, "sts")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {
        "adapter": "STSAdapter",
        "verification": True,
        "sts_module_loaded": True,
    }


def test_the_entrypoint_refuses_to_start_with_a_misspelled_value(tmp_path):
    done = start(tmp_path, "bogus")
    assert done.returncode != 0 and "APP_AWS_VERIFY must be empty or sts." in done.stderr

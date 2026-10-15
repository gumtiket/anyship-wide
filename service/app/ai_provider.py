"""bronze-ai를 웹 프로세스 밖에서, 최소한으로 명시한 환경 변수만으로 실행한다."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .analysis_contract import WorkerResult
from .analysis_source import AnalysisError


def worker_environment(work):
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL"}
    }
    env.update(
        PYTHONUTF8="1",
        PYTHONNOUSERSITE="1",
        PYTHONUNBUFFERED="1",
        AWS_SHARED_CREDENTIALS_FILE=str(work / "no-credentials"),
        AWS_CONFIG_FILE=str(work / "no-config"),
        AWS_EC2_METADATA_DISABLED="true",
    )
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL_ID_STRONG",
        "ANTHROPIC_MODEL_ID_FAST",
        "ANTHROPIC_EFFORT_STRONG",
        "ANTHROPIC_EFFORT_FAST",
        "ANTHROPIC_REFUSAL_FALLBACK",
        "BEDROCK_REGION",
        "BEDROCK_MODEL_ID_STRONG",
        "BEDROCK_MODEL_ID_FAST",
    ):
        if os.environ.get(key):
            env[key] = os.environ[key]
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
    ):
        if os.environ.get("APP_AI_" + key):
            env[key] = os.environ["APP_AI_" + key]
    if os.getenv("APP_AI_USE_INSTANCE_ROLE", "false").lower() == "true":
        env["AWS_EC2_METADATA_DISABLED"] = "false"
    return env


class ProcessAIProvider:
    def __init__(self, settings):
        self.settings = settings

    def run(self, work, request, log, cancelled):
        work = Path(work)
        request_file, result_file, logs = (
            work / "request.json",
            work / "result.json",
            work / "events.jsonl",
        )
        request_file.write_text(json.dumps(request), encoding="utf-8")
        started, offset = time.monotonic(), 0
        with logs.open("wb") as stream:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "app.analysis_worker",
                    str(work / "repo"),
                    str(work / "out"),
                    str(request_file),
                    str(result_file),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=worker_environment(work),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                while True:
                    if cancelled() or time.monotonic() - started > self.settings.ai_timeout:
                        raise AnalysisError(
                            "analysis_timeout", "분석 시간이 초과되거나 작업이 중단됐습니다."
                        )
                    if logs.stat().st_size > 64 * 1024:
                        raise AnalysisError(
                            "analysis_output_limit", "분석 출력 제한을 초과했습니다."
                        )
                    with logs.open("rb") as reader:
                        reader.seek(offset)
                        for line in reader:
                            if not line.endswith(b"\n"):
                                break
                            offset += len(line)
                            event = json.loads(line)
                            if event.get("stage") in {"분석 중", "검증 중", "실패"}:
                                log(event["stage"])
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                if (
                    process.returncode
                    or not result_file.is_file()
                    or result_file.stat().st_size > 4 * 1024 * 1024
                ):
                    raise AnalysisError(
                        "analysis_failed",
                        "AI 분석에 실패했습니다. 모델 설정과 입력 지원 범위를 확인해 주세요.",
                    )
                return WorkerResult.model_validate_json(result_file.read_bytes())
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)

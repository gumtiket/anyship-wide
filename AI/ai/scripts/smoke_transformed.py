"""신뢰된 샘플 사본에서, 접속할 수 없는 Postgres 주소로 health 확인을 한다."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import urlopen

from ai.detectors import RepoView
from ai.llm.base import LLMClient
from ai.models import AnalysisResult
from ai.pipeline import run_analysis
from ai.transform.workspace import Workspace, source_files


def health_check(name: str, llm: LLMClient | None = None) -> AnalysisResult:
    sample = Path(__file__).resolve().parents[2] / "samples" / name
    with TemporaryDirectory(prefix="bronze-p2-") as directory:
        result = run_analysis(sample, out_dir=Path(directory) / "out", llm=llm, log=lambda *_: None)
        report = result.transformation
        if report.sample_name != name or not report.patch_valid or not report.compile_passed:
            raise RuntimeError("검증된 자체 샘플 변경안만 실행할 수 있습니다.")
        diff = Path(result.output_files["changes.diff"]).read_text()
        with Workspace(source_files(RepoView(sample))) as workspace:
            workspace.apply(diff)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("AWS_", "BEDROCK_", "DATABASE_", "SECRET_"))
            }
            env.update(
                DATABASE_URL="postgresql://dummy:dummy-secret-do-not-use@127.0.0.1:1/bronze?connect_timeout=1",
                SECRET_KEY="dummy-secret-do-not-use",
                LOG_LEVEL="info",
                PYTHONDONTWRITEBYTECODE="1",
            )
            with (Path(directory) / "uvicorn.log").open("w") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    cwd=workspace.root,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError(f"{name}: 변환본 기동 실패")
                        try:
                            with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                                assert response.status == 200 and json.loads(response.read()) == {
                                    "status": "ok"
                                }
                            break
                        except (URLError, TimeoutError):
                            if time.monotonic() > deadline:
                                raise RuntimeError(f"{name}: healthz 대기 초과") from None
                            time.sleep(0.05)
                    assert not list(workspace.root.rglob("*.db"))
                    assert not (workspace.root / "app.log").exists()
                    print(f"{name}: diff 적용·uvicorn /healthz 200. Postgres 연결/CRUD는 미검증.")
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", choices=("todo", "todo-scheduler"), default="todo")
    health_check(parser.parse_args().sample)

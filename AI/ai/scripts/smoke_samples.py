"""임시 샘플 사본에 실제 uvicorn 프로세스를 띄워 확인한다. AWS·Docker는 쓰지 않는다."""

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import Request, urlopen


def request(
    base: str, path: str, method: str = "GET", data: dict | None = None
) -> tuple[int, object]:
    body = json.dumps(data).encode() if data is not None else None
    req = Request(
        base + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    with urlopen(req, timeout=2) as response:
        content = response.read()
        return response.status, json.loads(content) if content else None


def smoke(name: str) -> None:
    source = Path(__file__).resolve().parents[2] / "samples" / name
    with TemporaryDirectory(prefix="bronze-p1-") as directory:
        folder = Path(directory) / "app-copy"
        shutil.copytree(
            source,
            folder,
            ignore=shutil.ignore_patterns("*.db", "*.db-*", "app.log", "data", "__pycache__"),
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
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
                cwd=folder,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 10
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"{name}: uvicorn 시작 실패")
                    try:
                        status, initial = request(base, "/todos")
                        break
                    except (URLError, TimeoutError):
                        if time.monotonic() > deadline:
                            raise RuntimeError(f"{name}: 시작 대기 초과") from None
                        time.sleep(0.05)
                assert status == 200 and len(initial) == 2
                status, created = request(base, "/todos", "POST", {"title": "HTTP CRUD 확인"})
                assert status == 201
                todo_id = created["id"]
                status, updated = request(
                    base, f"/todos/{todo_id}", "PUT", {"title": "수정 확인", "done": True}
                )
                assert status == 200 and updated["done"]
                assert any(item["id"] == todo_id for item in request(base, "/todos")[1])
                assert request(base, "/export", "POST")[0] == 200
                assert request(base, f"/todos/{todo_id}", "DELETE")[0] == 204
                assert all(item["id"] != todo_id for item in request(base, "/todos")[1])
                print(f"{name}: 실제 uvicorn HTTP CRUD·내보내기 통과 (임시 복사본)")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    for sample in ("todo", "todo-scheduler"):
        smoke(sample)

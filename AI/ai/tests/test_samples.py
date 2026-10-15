import importlib
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


@contextmanager
def sample_client(name, tmp_path, monkeypatch):
    copy = tmp_path / "sample"
    shutil.copytree(SAMPLES / name, copy)
    monkeypatch.chdir(copy)
    monkeypatch.syspath_prepend(str(copy))
    for key in list(sys.modules):
        if key == "app" or key.startswith("app."):
            del sys.modules[key]
    module = importlib.import_module("app.main")
    try:
        with TestClient(module.app) as client:
            yield client, module, copy
    finally:
        for key in list(sys.modules):
            if key == "app" or key.startswith("app."):
                del sys.modules[key]


@pytest.mark.parametrize("name", ["todo", "todo-scheduler"])
def test_sample_crud_validation_export_and_no_healthz(tmp_path, monkeypatch, name):
    with sample_client(name, tmp_path, monkeypatch) as (client, module, copy):
        assert client.get("/").status_code == 200
        assert len(client.get("/todos").json()) == 2
        assert client.get("/healthz").status_code == 404
        assert client.post("/todos", json={"title": "   "}).status_code == 422
        created = client.post(
            "/todos", json={"title": "직접 확인", "due_at": "2099-01-01T09:00:00+09:00"}
        )
        assert created.status_code == 201
        todo = created.json()
        assert todo["due_at"] == "2099-01-01T00:00:00"
        todo_id = todo["id"]
        edited = client.put(f"/todos/{todo_id}", json={"title": "수정 확인", "done": True})
        assert edited.status_code == 200 and edited.json()["done"]
        assert client.post("/export").status_code == 200
        assert (copy / "data/todos.json").is_file()
        assert client.delete(f"/todos/{todo_id}").status_code == 204
        assert all(item["id"] != todo_id for item in client.get("/todos").json())
        assert client.delete(f"/todos/{todo_id}").status_code == 404
        assert client.put(f"/todos/{todo_id}", json={"title": "missing"}).status_code == 404


def test_scheduler_registers_one_minute_job_and_runs_in_background(tmp_path, monkeypatch):
    with sample_client("todo-scheduler", tmp_path, monkeypatch) as (client, module, copy):
        scheduler = module.app.state.scheduler
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1 and jobs[0].trigger.interval.total_seconds() == 60
        due = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        created = client.post("/todos", json={"title": "마감 검사", "due_at": due}).json()
        # 임시 테스트 사본의 트리거만 빠르게 한다. 운영 기본값은 1분 그대로다.
        scheduler.reschedule_job("mark-overdue", trigger="interval", seconds=0.05)
        limit = time.monotonic() + 3
        observed = False
        while time.monotonic() < limit:
            todo = next(t for t in client.get("/todos").json() if t["id"] == created["id"])
            if todo["overdue"]:
                observed = True
                break
            time.sleep(0.02)
        assert observed
    assert not scheduler.running

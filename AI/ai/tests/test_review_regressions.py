"""리뷰에서 찾은 결함마다 독립된 입력을 쓴다. 검사 대상 앱 코드는 실행하지 않는다."""

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from ai.detectors import RepoView, detect, detect_framework
from ai.gate.runner import CURL_IMAGE, FakeRunner
from ai.gate.service import run_gate
from ai.llm import FakeLLMClient
from ai.pipeline import OUTPUT_NAMES, run_analysis
from ai.security import DUMMY_SECRET, SourceMasker
from ai.spec.models import DeploySpec, Environment
from ai.spec.tfvars_schema import ServerlessVars
from ai.transform.workspace import Workspace, source_files

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "from fastapi import FastAPI\napp = FastAPI()\n"
# 꾸며 낸 값이며, 명시적으로 허용한 샘플 표식 예외에 해당하지 않는다.
REVIEW_SECRET = "dummy-review-secret-do-not-use"


def repo(tmp_path, source="", name="review-app", declaration=True):
    path = tmp_path / name
    path.mkdir()
    (path / "main.py").write_text(PREFIX + source)
    if declaration:
        (path / "requirements.txt").write_text("fastapi==0.142.4\nuvicorn==0.54.0\n")
    return path


def analyze(path, output, **kwargs):
    result = run_analysis(path, out_dir=output, log=lambda *_: None, **kwargs)
    if result.build_context:
        result.build_context.cleanup()
    return result


@pytest.mark.parametrize(
    "source",
    [
        'import redis\nredis.Redis(password="{secret}")\n',
        'import os\nurl=os.getenv("REDIS_URL", "redis://:{secret}@cache.invalid/0")\n',
        'from os import getenv as get\nvalue=get("TOKEN", "{secret}")\n',
        'from os import getenv as get\nvalue=get("TOKEN", default="{secret}")\n',
        'headers={{"Authorization":"Bearer {secret}"}}\n',
        'import httpx\nhttpx.BasicAuth("dummy-user", "{secret}")\n',
        'url="https://api.invalid/?access_token={secret}"\n',
        'import os\nvalue=os.getenv("HEADER", "Bearer {secret}")\n',
    ],
)
def test_secret_shapes_are_masked_before_fake_transport_and_all_outputs(tmp_path, source):
    path = repo(tmp_path, source.format(secret=REVIEW_SECRET))
    client = FakeLLMClient({"diagnose": ["{}"], "transform": ['{"diff":"","violation_ids":[]}']})
    result = analyze(path, tmp_path / "out", llm=client, save_llm_trace=True)
    assert all(REVIEW_SECRET not in call.user for call in client.calls)
    assert any(v.rule == "hardcoded_secret" for v in result.diagnosis.violations)
    for file in (tmp_path / "out").iterdir():
        assert REVIEW_SECRET not in file.read_text(), file.name


def test_json_credentials_are_redacted_if_llm_repeats_them(tmp_path):
    path = repo(tmp_path)
    (path / "config.json").write_text(json.dumps({"nested": {"password": DUMMY_SECRET}}))
    masker = SourceMasker(RepoView(path))
    assert masker.text(DUMMY_SECRET) == "[REDACTED]"
    assert "config.json" in masker.blocked_files


def test_sensitive_ignore_input_and_uppercase_env_are_not_exported(tmp_path):
    path = repo(tmp_path)
    (path / ".ENV").write_text(f"TOKEN={REVIEW_SECRET}")
    (path / ".dockerignore").write_text(f"https://dummy:{REVIEW_SECRET}@private.invalid\n")
    assert ".ENV" not in RepoView(path).files()
    result = analyze(path, tmp_path / "out")
    assert result.deploy_spec is None and result.status == "partial"
    assert all(REVIEW_SECRET not in file.read_text() for file in (tmp_path / "out").iterdir())


def test_existing_database_url_without_database_evidence_blocks_packaging(tmp_path):
    path = repo(
        tmp_path,
        "import os\nfrom sqlalchemy import create_engine\n"
        'engine=create_engine(os.environ["DATABASE_URL"])\n',
    )
    result = analyze(path, tmp_path / "out")
    assert result.deploy_spec is None and result.status == "failed"
    assert "postgres_binding_unresolved" in {w.code for w in result.packaging_warnings}
    assert "deployment_contract" in result.recommendation.needs_confirmation


def test_existing_secret_key_is_not_generated_even_when_another_secret_is_extracted(tmp_path):
    path = repo(
        tmp_path, f'import os\nSECRET_KEY=os.environ["SECRET_KEY"]\nOTHER_TOKEN="{DUMMY_SECRET}"\n'
    )
    result = analyze(path, tmp_path / "out")
    assert all(not e.generate for e in result.deploy_spec.env)


@pytest.mark.parametrize(
    "source",
    [
        'import sqlite3\nconn=sqlite3.connect("app.db")\n',
        'from sqlite3 import connect as connect_db\nconn=connect_db("app.db")\n',
        'import aiosqlite as db\nconn=db.connect("app.db")\n',
    ],
)
def test_direct_sqlite_requires_review_and_does_not_claim_stateless(tmp_path, source):
    path = repo(tmp_path, source)
    result = analyze(path, tmp_path / "out")
    finding = next(v for v in result.diagnosis.violations if v.rule == "sqlite_usage")
    assert finding.change_class == "risky" and not finding.auto_fixable
    assert next(r.status for r in result.diagnosis.factor_reviews if r.factor == 6) == "violation"
    assert result.gate_report.status == "skipped" and not result.gate_report.pr_eligible


def test_explicit_in_memory_sqlite_is_not_persistent_file_storage(tmp_path):
    path = repo(tmp_path, 'import sqlite3\nconn=sqlite3.connect(":memory:")\n')
    assert not any(v.rule == "sqlite_usage" for v in detect(RepoView(path)))


def test_postgres_driver_prefix_substitution_is_not_a_hardcoded_database_address(tmp_path):
    path = repo(
        tmp_path,
        "import os\nfrom sqlalchemy import create_engine\n"
        'engine=create_engine(os.environ["DATABASE_URL"].replace('
        '"postgresql://", "postgresql+psycopg2://", 1))\n',
    )
    assert not any(v.rule == "hardcoded_db_url" for v in detect(RepoView(path)))


@pytest.mark.parametrize(
    "seconds,expected", [(25, "aws-serverless"), (30, "aws-serverless"), (31, "aws-always-on")]
)
def test_mvp_request_boundary_is_explicit_input_not_llm_choice(tmp_path, seconds, expected):
    path = repo(tmp_path)
    client = FakeLLMClient(
        {"dockerfile": ["{}"], "spec": ['{"max_request_seconds":60,"ingress":"internal"}']}
    )
    result = analyze(path, tmp_path / "out", artifact_llm=client, max_request_seconds=seconds)
    assert result.recommendation.set == expected
    assert result.deploy_spec.workload.max_request_seconds == seconds
    assert result.deploy_spec.ingress == "public"


def test_long_request_signal_alone_prevents_serverless(tmp_path):
    path = repo(tmp_path, "import time\ndef work():\n    time.sleep(31)\n")
    result = analyze(path, tmp_path / "out")
    assert result.recommendation.set == "aws-always-on"
    assert result.recommendation.rule_fired == "long_request_signal"


@pytest.mark.parametrize(
    "name",
    [
        "PORT",
        "LD_PRELOAD",
        "PATH",
        "AWS_ACCESS_KEY_ID",
        "DOCKER_HOST",
        "LAMBDA_TASK_ROOT",
        "PYTHONPATH",
        "_HANDLER",
    ],
)
def test_environment_reserved_names_rejected_by_model_and_schema(name):
    data = {"name": name, "value": "dummy"}
    with pytest.raises(ValidationError):
        Environment.model_validate(data)
    assert list(Draft202012Validator(Environment.model_json_schema()).iter_errors(data))


@pytest.mark.parametrize(
    "data",
    [
        {"name": "API_TOKEN", "secret": True, "value": DUMMY_SECRET},
        {"name": "LOG_LEVEL", "generate": True, "value": "info"},
        {"name": "DATABASE_URL", "secret": True, "generate": True},
        {"name": "LOG_LEVEL"},
        {"name": "LOG_LEVEL", "value": "info\nOTHER=yes"},
        {"name": "LOG_LEVEL", "value": "info\n"},
        {"name": "LOG_LEVEL", "value": "info\r"},
        {"name": "LOG_LEVEL", "value": "info\x00"},
    ],
)
def test_env_combinations_and_injection_rejected_by_both_validators(data):
    with pytest.raises(ValidationError):
        Environment.model_validate(data)
    assert list(Draft202012Validator(Environment.model_json_schema()).iter_errors(data))


def test_env_utf8_budget_and_duplicate_names(tmp_path):
    result = analyze(ROOT / "samples/todo", tmp_path / "out")
    data = result.deploy_spec.model_dump(mode="json", exclude_none=True)
    Draft202012Validator(DeploySpec.model_json_schema()).validate(
        result.deploy_spec.model_dump(mode="json")
    )
    with pytest.raises(ValidationError, match="environment_total_size_exceeded"):
        DeploySpec.model_validate(
            {
                **data,
                "env": [
                    {"name": "FIRST", "value": "가" * 800},
                    {"name": "SECOND", "value": "나" * 800},
                ],
            }
        )
    with pytest.raises(ValidationError, match="duplicate_environment"):
        DeploySpec.model_validate(
            {
                **data,
                "env": [
                    {"name": "LOG_LEVEL", "value": "info"},
                    {"name": "LOG_LEVEL", "value": "debug"},
                ],
            }
        )
    with pytest.raises(ValidationError):
        ServerlessVars(timeout_s=31)
    with pytest.raises(ValidationError, match="object_storage_mvp_unsupported"):
        DeploySpec.model_validate(
            {**data, "backing_services": [{"type": "object_storage", "bind_as": "STORAGE_URL"}]}
        )


def test_invalid_env_defaults_and_missing_values_require_confirmation(tmp_path):
    path = repo(
        tmp_path,
        'import os\nmode=os.getenv("APP_MODE")\nvalue=os.getenv("LOG_LEVEL", "info\\nOTHER=yes")\n',
    )
    result = analyze(path, tmp_path / "out")
    assert not result.deploy_spec.env
    assert {"environment_value_required", "environment_value_unsafe"} <= set(
        result.recommendation.needs_confirmation
    )


def test_conflicting_defaults_need_user_input_and_secret_use_wins(tmp_path):
    path = repo(
        tmp_path,
        'import os\na=os.getenv("APP_MODE","first")\n'
        'b=os.getenv("APP_MODE","second")\n'
        f'c=os.getenv("HEADER","public")\nd=os.getenv("HEADER","Bearer {REVIEW_SECRET}")\n',
    )
    result = analyze(path, tmp_path / "out")
    assert not any(e.name == "APP_MODE" for e in result.deploy_spec.env)
    header = next(e for e in result.deploy_spec.env if e.name == "HEADER")
    assert header.secret and header.value is None and not header.generate


def test_app_name_input_and_private_local_source_default(tmp_path):
    path = repo(tmp_path, name="My_Repo.v2")
    result = analyze(path, tmp_path / "out")
    assert result.deploy_spec.app == "my-repo-v2"
    assert result.deploy_spec.source.repo == "local://my-repo-v2"
    named = analyze(
        path, tmp_path / "named", app_name="todo-a1b2", source_repo="https://github.com/team/todo"
    )
    assert named.deploy_spec.app == "todo-a1b2"
    with pytest.raises(ValueError, match="app_name"):
        analyze(path, tmp_path / "bad", app_name="My_Repo.v2")
    assert not (tmp_path / "bad").exists()


def test_new_out_policy_preserves_previous_result_and_never_calls_llm(tmp_path):
    path = repo(tmp_path)
    result = analyze(path, tmp_path / "out")
    before = {p.name: p.read_bytes() for p in (tmp_path / "out").iterdir()}
    client = FakeLLMClient([])
    with pytest.raises(ValueError, match="output_not_empty"):
        analyze(path, tmp_path / "out", llm=client)
    assert not client.calls
    assert before == {p.name: p.read_bytes() for p in (tmp_path / "out").iterdir()}
    assert all(Path(p).exists() for p in result.output_files.values())


def test_concurrent_client_sharing_is_rejected_before_second_request(tmp_path):
    started, release = Event(), Event()

    def response(system, user):
        started.set()
        assert release.wait(10)
        return "{}"

    client = FakeLLMClient({"diagnose": [response, "{}"]})
    path = repo(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(analyze, path, tmp_path / "first", llm=client, save_llm_trace=True)
        assert started.wait(10)
        try:
            with pytest.raises(ValueError, match="llm_client_in_use"):
                analyze(path, tmp_path / "second", llm=client, save_llm_trace=True)
            assert not (tmp_path / "second").exists()
        finally:
            release.set()
        first = future.result()
    second = analyze(path, tmp_path / "second", llm=client)
    assert first.cost.total.calls == second.cost.total.calls == 1
    assert first.cost.calls[0].stage == "diagnose"


def test_dockerignore_is_in_diff_and_blocks_late_reinclude(tmp_path):
    path = repo(tmp_path)
    (path / ".dockerignore").write_text("custom-assets\n!.env\n!*.db\n")
    result = analyze(path, tmp_path / "out")
    diff = Path(result.output_files["changes.diff"]).read_text()
    assert "b/.dockerignore" in diff
    with Workspace(source_files(RepoView(path))) as workspace:
        workspace.apply(diff)
        ignore = (workspace.root / ".dockerignore").read_text()
        assert "custom-assets" in ignore and ignore.rindex(".[eE][nN][vV]*\n") > ignore.index(
            "!.env"
        )
        assert (tmp_path / "out/.dockerignore").read_text() == ignore


def test_owned_sample_build_context_uses_the_same_ignore_as_the_proposed_patch(tmp_path):
    path = tmp_path / "todo"
    shutil.copytree(ROOT / "samples/todo", path)
    (path / ".dockerignore").write_text("custom-assets\n!.env\n")
    result = run_analysis(path, out_dir=tmp_path / "out", log=lambda *_: None)
    try:
        assert (Path(result.build_context.root) / ".dockerignore").read_text() == (
            tmp_path / "out/.dockerignore"
        ).read_text()
    finally:
        result.build_context.cleanup()


def test_starlette_helpers_do_not_mean_independent_starlette_app(tmp_path):
    path = repo(tmp_path, "from starlette.responses import JSONResponse\n")
    assert detect_framework(RepoView(path)).support_grade == "supported"
    with (path / "main.py").open("a") as stream:
        stream.write("from starlette.applications import Starlette\nother=Starlette()\n")
    assert detect_framework(RepoView(path)).support_grade == "partial"


@pytest.mark.parametrize("declaration", ["none", "pyproject"])
def test_dependency_declaration_and_packaging_support_are_distinct(tmp_path, declaration):
    path = repo(tmp_path, declaration=False)
    if declaration == "pyproject":
        (path / "pyproject.toml").write_text('[project]\ndependencies=["fastapi==0.142.4"]\n')
    result = analyze(path, tmp_path / "out")
    assert result.deploy_spec is None and result.status == "partial"
    assert "unsupported" in Path(result.output_files["Dockerfile"]).read_text()
    assert any(v.rule == "missing_dependency_declaration" for v in result.diagnosis.violations) == (
        declaration == "none"
    )
    assert all(Path(result.output_files[name]).exists() for name in OUTPUT_NAMES)


def test_non_object_health_json_fails_report_and_cleans_fake_resources(tmp_path):
    class NonObject(FakeRunner):
        def logs(self, name):
            image, command = self.containers[name]
            if image == CURL_IMAGE and any(word.endswith("/healthz") for word in command):
                return "[]"
            return super().logs(name)

    result = run_analysis(ROOT / "samples/todo", out_dir=tmp_path / "out", log=lambda *_: None)
    runner = NonObject()
    try:
        report = run_gate(
            result.build_context,
            result.deploy_spec,
            runner,
            lambda *_: None,
            timeout_s=1,
            sleep=lambda _: None,
        )
        assert report.status == "failed"
        assert not runner.containers and not runner.networks and not runner.images
    finally:
        result.build_context.cleanup()


def test_empty_dependency_file_is_not_a_declaration(tmp_path):
    path = repo(tmp_path)
    (path / "requirements.txt").write_text("# TODO declare dependencies\n")
    result = analyze(path, tmp_path / "out")
    assert result.deploy_spec is None
    assert any(v.rule == "missing_dependency_declaration" for v in result.diagnosis.violations)


def test_unexpected_exception_cleans_context_and_publishes_no_partial_output(tmp_path, monkeypatch):
    contexts = []
    from ai.transform.context import prepare_context

    def remember(*args, **kwargs):
        context = prepare_context(*args, **kwargs)
        contexts.append(context)
        return context

    class Explodes(FakeRunner):
        def preflight(self):
            raise RuntimeError("review-fake-failure")

    monkeypatch.setattr("ai.pipeline.prepare_context", remember)
    with pytest.raises(RuntimeError):
        run_analysis(
            ROOT / "samples/todo", out_dir=tmp_path / "out", runner=Explodes(), log=lambda *_: None
        )
    assert contexts and all(not Path(c.root).exists() for c in contexts)
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".bronze-*"))

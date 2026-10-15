import json
import re
import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel

from ai.cli import main
from ai.demo_cache import DEFAULT_CACHE, engine_hash, save_cache
from ai.detectors import RepoView
from ai.gate.runner import FakeRunner
from ai.golden import core_result
from ai.llm.fake import FakeLLMClient
from ai.llm.recording import (
    DEFAULT_FIXTURES,
    PlaybackError,
    RecordingClient,
    ReplayClient,
    assert_public,
)
from ai.pipeline import OUTPUT_NAMES, run_analysis
from ai.transform.workspace import Workspace, source_files

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "samples/todo"


class Message(BaseModel):
    message: str


def test_record_and_replay_preserve_schema_retry_sequence_without_external_usage(tmp_path):
    backend = FakeLLMClient(["{}", '{"message":"ok"}'])
    record = RecordingClient(backend, SAMPLE, tmp_path / "llm")
    first = record.complete("system", "user", tier="fast", schema=Message, stage="diagnose")
    replay = ReplayClient(SAMPLE, tmp_path / "llm", provider="fake")
    second = replay.complete("system", "user", tier="fast", schema=Message, stage="diagnose")
    replay.assert_consumed()
    assert first.parsed == second.parsed
    assert len(backend.calls) == 2
    assert replay.tracker.report().total.calls == 2
    assert replay.tracker.report().total.input_tokens == 0
    assert replay.tracker.report().total.cost_usd == 0
    assert second.transport_attempts == 0
    fixture = json.loads((tmp_path / "llm/fake/todo/diagnose.json").read_text())
    assert len(fixture["entries"]) == 2
    assert fixture["entries"][0]["prompt_hash"] != fixture["entries"][1]["prompt_hash"]
    assert "request_system" not in fixture and "request_user" not in fixture


def test_prompt_or_schema_changes_are_fatal_not_normal_llm_fallback(tmp_path):
    backend = FakeLLMClient(['{"message":"ok"}'])
    RecordingClient(backend, SAMPLE, tmp_path / "llm").complete(
        "system", "user", tier="fast", schema=Message, stage="diagnose"
    )
    replay = ReplayClient(SAMPLE, tmp_path / "llm", provider="fake")
    with pytest.raises(PlaybackError, match="prompt_hash_mismatch"):
        replay.complete("changed system", "user", tier="fast", schema=Message, stage="diagnose")
    with pytest.raises(PlaybackError, match="prompt_hash_mismatch"):
        replay.complete("system", "user", tier="fast", schema=None, stage="diagnose")


def test_fixture_response_tampering_and_unused_records_are_rejected(tmp_path):
    RecordingClient(FakeLLMClient(['{"message":"ok"}']), SAMPLE, tmp_path / "llm").complete(
        "system", "user", tier="fast", schema=Message, stage="diagnose"
    )
    replay = ReplayClient(SAMPLE, tmp_path / "llm", provider="fake")
    with pytest.raises(PlaybackError, match="unused"):
        replay.assert_consumed()
    path = tmp_path / "llm/fake/todo/diagnose.json"
    fixture = json.loads(path.read_text())
    fixture["entries"][0]["response"]["text"] = '{"message":"edited"}'
    path.write_text(json.dumps(fixture))
    with pytest.raises(PlaybackError, match="response_hash_mismatch"):
        ReplayClient(SAMPLE, tmp_path / "llm", provider="fake")


def test_credentials_redacted_before_record_parse_and_storage(tmp_path):
    credential_url = "postgresql://test-user:test-password@db.invalid/demo"
    record = RecordingClient(
        FakeLLMClient([json.dumps({"message": credential_url})]), SAMPLE, tmp_path / "llm"
    )
    result = record.complete("system", "user", tier="fast", schema=Message, stage="diagnose")
    text = (tmp_path / "llm/fake/todo/diagnose.json").read_text()
    assert "test-password" not in text and "test-user" not in text
    assert "[REDACTED]" in result.parsed.message
    assert_public(text)


@pytest.mark.parametrize(
    "text",
    [
        "AK" + "IA" + "Z" * 16,
        "ghp_" + "z" * 30,
        'PASSWORD="fabricated-unsafe-test-value"',
        'api_key="fabricated-unsafe-test-value"',
        "-----BEGIN " + "PRIVATE KEY-----",
        "123" + "456" + "789" + "012",
    ],
)
def test_public_fixture_guard_rejects_credential_patterns(text):
    with pytest.raises(PlaybackError, match="sensitive_material"):
        assert_public(text)


def test_hash_digits_are_not_mistaken_for_account_ids():
    digits = "123456" + "789012"
    assert_public("bronze-ai-gate:gate-abcdef" + digits + "abcdef")
    with pytest.raises(PlaybackError):
        assert_public("arn:aws:iam::" + digits + ":user/dummy")


def test_record_rejects_non_sample_and_output_inside_original(tmp_path):
    backend = FakeLLMClient([])
    with pytest.raises(PlaybackError, match="자체 샘플"):
        RecordingClient(backend, ROOT / "samples/memo-app", tmp_path / "llm")
    with pytest.raises(PlaybackError, match="원본 보호"):
        RecordingClient(backend, SAMPLE, SAMPLE / "fixtures")
    assert not backend.calls


@pytest.mark.parametrize(
    "name,expected", [("todo", "aws-serverless"), ("todo-scheduler", "aws-always-on")]
)
def test_recorded_golden_pipeline_and_same_input_dockerfile_bytes(tmp_path, name, expected):
    repo = ROOT / "samples" / name
    expected_core = json.loads((ROOT / "ai/tests/fixtures/golden" / f"{name}.json").read_text())
    # 배포 쪽 2026-10-10 계약에서 앱별 컨테이너 tfvars가 폐지되었다. 과거 골든 파일은
    # 그대로 두고 나머지 필드는 모두 정확히 비교한다.
    if expected == "aws-always-on":
        expected_core["tfvars"] = {}
        expected_core["needs_confirmation"] = [
            key for key in expected_core["needs_confirmation"] if key != "tfvars_schema"
        ]
    outputs = []
    for number in range(2):
        replay = ReplayClient(repo)
        result = run_analysis(
            repo,
            out_dir=tmp_path / str(number),
            llm=replay,
            decision_llm=replay,
            runner=FakeRunner(),
            log=lambda *_: None,
        )
        try:
            replay.assert_consumed()
            assert core_result(result) == expected_core
            assert result.recommendation.set == expected
            assert result.execution_source == "llm_replay"
            assert result.cost.external_calls == 0 and result.cost.total.input_tokens == 0
            assert result.gate_report.status == "skipped" and not result.gate_report.pr_eligible
            with Workspace(source_files(RepoView(repo))) as workspace:
                workspace.apply(Path(result.output_files["changes.diff"]).read_text())
                assert workspace.compile()
            outputs.append(Path(result.output_files["Dockerfile"]).read_bytes())
        finally:
            result.build_context.cleanup()
    assert outputs[0] == outputs[1]


def test_committed_recordings_contain_no_credentials():
    for path in DEFAULT_FIXTURES.rglob("*.json"):
        fixture = json.loads(path.read_text())
        assert fixture["origin"] == "BedrockClient"
        assert_public(path.read_text())
        if path.name == "manifest.json":
            continue
        for entry in fixture["entries"]:
            assert_public(entry["response"]["text"])


class MustNotCall:
    def complete(self, *args, **kwargs):
        raise AssertionError("Cache hit must never call LLM")


@pytest.mark.parametrize("sample", ["todo", "todo-scheduler"])
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "저장된 데모 캐시는 LLM 범위 검사 변경 전 엔진의 결과입니다. "
        "사용자 확인 후 replay + Docker로 재생성하고 이 임시 xfail을 제거해야 합니다."
    ),
)
def test_stored_demo_cache_matches_current_engine(sample):
    # mock 없이, 알려진 오래된 캐시를 재생 동작 테스트와 따로 드러낸다.
    manifest = json.loads((DEFAULT_CACHE / sample / "manifest.json").read_text())
    assert manifest["engine_hash"] == engine_hash(), f"{sample}: cache_engine_hash_mismatch"


def test_recorded_demo_cache_hit_with_matching_engine_is_historical(tmp_path, monkeypatch):
    manifest = json.loads((DEFAULT_CACHE / "todo/manifest.json").read_text())
    # 녹화된 엔진 버전으로 재생을 확인한다. 현재 엔진이 Docker를 통과했다고 주장하려고
    # 과거 manifest를 고쳐 쓰지 않는다. 불일치 테스트는 따로 둔다.
    monkeypatch.setattr("ai.demo_cache.engine_hash", lambda: manifest["engine_hash"])
    logs = []
    result = run_analysis(
        SAMPLE,
        out_dir=tmp_path / "out",
        use_demo_cache=True,
        llm=MustNotCall(),
        runner=MustNotCall(),
        log=lambda *args: logs.append(args),
    )
    assert result.execution_source == "demo_cache"
    assert result.build_context is None
    assert result.gate_report.status == "skipped"
    assert result.gate_report.historical_status == "passed"
    assert result.gate_report.execution_source == "demo_cache"
    assert not result.gate_report.pr_eligible
    assert result.cost.historical and result.cost.external_calls == 0
    assert all("(사전 실행 결과)" in message for _, message in logs)
    assert all((tmp_path / "out" / name).exists() for name in OUTPUT_NAMES)
    assert [stage.value for stage, _ in logs[1:]] == [e["stage"] for e in manifest["events"]]
    assert not any(re.search(r"\bP\d+(?::| 결과:)", message) for _, message in logs)
    assert "규칙 진단·변경안·패키징·추천 시작" in logs[1][1]


@pytest.mark.parametrize("change", ["code", "env", "commit", "engine", "corrupt", "missing"])
def test_cache_mismatch_falls_back_with_explicit_log(tmp_path, monkeypatch, change):
    repo = tmp_path / "todo"
    shutil.copytree(SAMPLE, repo)
    cache = tmp_path / "cache"
    shutil.copytree(DEFAULT_CACHE, cache)
    args = {}
    if change == "code":
        path = repo / "app/main.py"
        path.write_text(path.read_text() + "\n# input code changed\n")
    elif change == "env":
        args["target_env"] = "onprem"
    elif change == "commit":
        args["commit"] = "a" * 40
    elif change == "engine":
        monkeypatch.setattr("ai.demo_cache.engine_hash", lambda: "changed")
    elif change == "corrupt":
        (cache / "todo/out/changes.diff").write_text("tampered")
    elif change == "missing":
        shutil.rmtree(cache)
    logs = []
    result = run_analysis(
        repo,
        out_dir=tmp_path / "out",
        use_demo_cache=True,
        demo_cache_dir=cache,
        log=lambda *args: logs.append(args),
        **args,
    )
    try:
        assert result.execution_source == "current_run"
        assert "정상 분석" in logs[0][1]
        if change == "env":
            assert result.recommendation.set == "onprem"
    finally:
        if result.build_context:
            result.build_context.cleanup()


def test_cache_never_overwrites_input_and_fake_result_cannot_be_published_as_live_cache(tmp_path):
    with pytest.raises(ValueError, match="원본 보호"):
        run_analysis(SAMPLE, out_dir=SAMPLE / "out", use_demo_cache=True)
    result = run_analysis(
        SAMPLE, out_dir=tmp_path / "out", runner=FakeRunner(), log=lambda *_: None
    )
    try:
        with pytest.raises(ValueError, match="actual_provider_and_docker"):
            save_cache(SAMPLE, result, [], root=tmp_path / "cache")
    finally:
        result.build_context.cleanup()


def test_replay_cli_is_offline_and_missing_fixture_is_clear_error(tmp_path, capsys):
    assert main(["analyze", str(SAMPLE), "--llm", "replay", "--out", str(tmp_path / "out")]) == 0
    assert (
        main(
            [
                "analyze",
                str(SAMPLE),
                "--llm",
                "replay",
                "--llm-fixtures",
                str(tmp_path / "missing"),
                "--out",
                str(tmp_path / "error"),
            ]
        )
        == 2
    )
    assert "fixture_missing" in capsys.readouterr().err
    assert not (tmp_path / "error").exists()

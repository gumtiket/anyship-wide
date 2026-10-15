"""데모 계약: 모델 검토가 규칙을 바꿀 수 없고, 재생은 제공자를 넘나들 수 없다."""

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import BaseModel

from ai.demo_cache import DEMO_ENVIRONMENTS, save_cache, settings_key
from ai.detectors import RepoView
from ai.gate.runner import FakeRunner
from ai.golden import core_result
from ai.llm import AnthropicClient, BedrockClient, FakeLLMClient, LLMResult
from ai.llm.fake import recommendation_response
from ai.llm.recording import PlaybackError, RecordingClient, ReplayClient, digest
from ai.models import ReviewCandidate, WarningItem
from ai.pipeline import run_analysis

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "samples/todo"
ANTHROPIC_ENV = {
    "ANTHROPIC_MODEL_ID_FAST": "claude-haiku-5-5",
    "ANTHROPIC_MODEL_ID_STRONG": "claude-sonnet-5-5",
}
BEDROCK_ENV = {
    "BEDROCK_REGION": "ap-northeast-2",
    "BEDROCK_MODEL_ID_FAST": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "BEDROCK_MODEL_ID_STRONG": "global.anthropic.claude-sonnet-4-6",
}


class Message(BaseModel):
    message: str


def backend(provider):
    sdk = Mock()
    if provider == "anthropic":
        sdk.messages.create.return_value = SimpleNamespace(
            id="offline-response",
            model="claude-haiku-5-5",
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"message":"ok"}')],
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        )
        return AnthropicClient(client=sdk, environ=ANTHROPIC_ENV), sdk
    sdk.converse.return_value = {
        "output": {"message": {"content": [{"text": '{"message":"ok"}'}]}},
        "usage": {"inputTokens": 3, "outputTokens": 2},
    }
    return BedrockClient(client=sdk, environ=BEDROCK_ENV), sdk


@pytest.mark.parametrize("sample,env", [("todo", "onprem"), ("todo-scheduler", "aws")])
def test_candidates_do_not_change_rules_transform_or_recommendation(tmp_path, sample, env):
    repo = ROOT / "samples" / sample
    evidence = RepoView(repo).read("app/main.py").splitlines()[0]
    candidate = {
        "factor": 2,
        "file": "app/main.py",
        "line": 1,
        "evidence": evidence,
        "description": "검토가 필요한 후보",
    }
    results = []
    try:
        for candidates in ([], [candidate]):
            fake = FakeLLMClient(
                {
                    "diagnose": [json.dumps({"explanations": [], "candidates": candidates})],
                    "recommend": [recommendation_response],
                }
            )
            result = run_analysis(
                repo,
                target_env=env,
                out_dir=tmp_path / str(len(results)),
                llm=fake,
                decision_llm=fake,
                log=lambda *_: None,
            )
            results.append(result)
        plain, reviewed = results
        assert len(reviewed.diagnosis.review_candidates) == 1
        assert all(v.source == "rule" for v in reviewed.diagnosis.violations)
        assert plain.diagnosis.violations == reviewed.diagnosis.violations
        assert plain.diagnosis.factor_reviews == reviewed.diagnosis.factor_reviews
        assert plain.transformation == reviewed.transformation
        assert plain.recommendation.set == reviewed.recommendation.set
        assert plain.status == reviewed.status
        assert core_result(plain) == core_result(reviewed)
        data = json.loads(Path(reviewed.output_files["diagnosis.json"]).read_text())
        assert data["review_candidates"][0]["line"] == 1
        assert data["review_candidates"][0]["confidence"] == "needs_review"
    finally:
        for result in results:
            result.build_context.cleanup()


@pytest.mark.parametrize(
    "change,code",
    [
        ({"factor": 5}, "llm_candidate_rejected"),
        ({"file": "../outside.py"}, "llm_candidate_rejected"),
        ({"line": 99999}, "llm_candidate_rejected"),
        ({"evidence": "not present in this source line"}, "llm_candidate_evidence_mismatch"),
    ],
)
def test_unverified_candidate_locations_are_not_published(tmp_path, change, code):
    candidate = {
        "factor": 2,
        "file": "app/main.py",
        "line": 1,
        "evidence": "import json",
        "description": "검토 후보",
    } | change
    result = run_analysis(
        SAMPLE,
        out_dir=tmp_path / "out",
        llm=FakeLLMClient([json.dumps({"explanations": [], "candidates": [candidate]})]),
        log=lambda *_: None,
    )
    try:
        assert not result.diagnosis.review_candidates
        assert code in {w.code for w in result.diagnosis.warnings}
        assert all(not v.startswith("llm_candidate:") for v in result.transformation.deferred_ids)
    finally:
        result.build_context.cleanup()


def test_golden_excludes_review_and_explanation_but_keeps_warning_and_artifact_checks(tmp_path):
    result = run_analysis(SAMPLE, out_dir=tmp_path / "out", log=lambda *_: None)
    try:
        original = core_result(result)
        result.diagnosis.review_candidates.append(
            ReviewCandidate(
                id="review",
                factor=2,
                file="app/main.py",
                line=1,
                evidence="import json",
                description="AI 설명",
            )
        )
        result.diagnosis.violations[0].description = "다른 LLM 설명"
        result.diagnosis.violations[0].impact = "다른 LLM 영향"
        assert core_result(result) == original
        result.transformation.warnings.append(WarningItem(code="new_warning", message="경고"))
        assert core_result(result) != original
        result.transformation.warnings.pop()
        path = Path(result.output_files["changes.diff"])
        path.write_text(path.read_text() + "\nchanged\n")
        assert core_result(result)["diff_sha256"] != original["diff_sha256"]
        path = Path(result.output_files["Dockerfile"])
        path.write_text(path.read_text() + "\n# changed\n")
        assert core_result(result)["dockerfile_sha256"] != original["dockerfile_sha256"]
    finally:
        result.build_context.cleanup()


def test_user_logs_and_reasons_have_no_milestone_prefixes(tmp_path):
    logs = []
    result = run_analysis(
        SAMPLE,
        out_dir=tmp_path / "out",
        runner=FakeRunner(),
        log=lambda _, message: logs.append(message),
    )
    try:
        assert not any(re.search(r"\bP\d+(?::| 결과:)", m) for m in logs)
        assert not re.search(r"\bP\d+:", result.gate_report.reason)
        assert any("규칙 위반 6개, AI 검토 후보 0개" in m for m in logs)
    finally:
        result.build_context.cleanup()


@pytest.mark.parametrize("provider", ["bedrock", "anthropic"])
def test_provider_recording_and_replay_are_offline_and_namespaced(tmp_path, provider):
    client, sdk = backend(provider)
    record = RecordingClient(client, SAMPLE, tmp_path)
    first = record.complete("system", "user", tier="fast", schema=Message, stage="diagnose")
    replay = ReplayClient(
        SAMPLE,
        tmp_path,
        provider=provider,
        expected_models=record.models,
        expected_parameters=record.parameters,
    )
    second = replay.complete("system", "user", tier="fast", schema=Message, stage="diagnose")
    replay.assert_consumed()
    assert first.parsed == second.parsed
    assert second.input_tokens == second.output_tokens == second.transport_attempts == 0
    assert (tmp_path / provider / "todo/manifest.json").exists()
    assert (
        sdk.messages.create.call_count == 1
        if provider == "anthropic"
        else sdk.converse.call_count == 1
    )


def record_anthropic(tmp_path):
    client, _ = backend("anthropic")
    record = RecordingClient(client, SAMPLE, tmp_path)
    record.complete("system", "user", tier="fast", schema=Message, stage="diagnose")
    return record


@pytest.mark.parametrize(
    "tamper,code",
    [
        ("origin", "provider"),
        ("model", "model"),
        ("parameters", "parameters"),
        ("entry_parameters", "parameters"),
        ("manifest_origin", "provider"),
    ],
)
def test_provider_model_and_parameter_mismatches_are_fatal(tmp_path, tamper, code):
    record_anthropic(tmp_path)
    path = tmp_path / "anthropic/todo/diagnose.json"
    data = json.loads(path.read_text())
    if tamper == "origin":
        data["origin"] = "BedrockClient"
    elif tamper == "model":
        entry = data["entries"][0]
        entry["response"]["model_id"] = "claude-sonnet-5-5"
        entry["response_hash"] = digest(entry["response"])
    elif tamper == "parameters":
        data["parameters"]["max_tokens"] += 1
    elif tamper == "entry_parameters":
        data["entries"][0]["request_parameters"]["max_tokens"] += 1
    else:
        path = tmp_path / "anthropic/todo/manifest.json"
        data = json.loads(path.read_text())
        data["origin"] = "BedrockClient"
    path.write_text(json.dumps(data))
    with pytest.raises(PlaybackError, match=code):
        ReplayClient(SAMPLE, tmp_path, provider="anthropic")


def test_replay_expected_configuration_and_prompt_are_checked(tmp_path):
    record = record_anthropic(tmp_path)
    with pytest.raises(PlaybackError, match="model"):
        ReplayClient(
            SAMPLE, tmp_path, provider="anthropic", expected_models={"fast": "claude-sonnet-5-5"}
        )
    with pytest.raises(PlaybackError, match="parameters"):
        ReplayClient(SAMPLE, tmp_path, provider="anthropic", expected_parameters={"fast": {}})
    with pytest.raises(PlaybackError, match="fixture_missing"):
        ReplayClient(SAMPLE, tmp_path, provider="bedrock")
    replay = ReplayClient(SAMPLE, tmp_path, provider="anthropic", expected_models=record.models)
    with pytest.raises(PlaybackError, match="prompt_hash_mismatch"):
        replay.complete("system", "different user", tier="fast", schema=Message, stage="diagnose")


def test_live_configuration_changes_are_rejected_before_another_transport_call(tmp_path):
    client, sdk = backend("anthropic")
    record = RecordingClient(client, SAMPLE, tmp_path)
    client.max_tokens += 1
    with pytest.raises(PlaybackError, match="configuration_changed"):
        record.complete("system", "user", tier="fast", schema=Message, stage="diagnose")
    assert not sdk.messages.create.called


@pytest.mark.parametrize("fallback", ["default", "off"])
def test_recorded_server_fallback_requires_matching_request_policy(tmp_path, fallback):
    client = AnthropicClient(
        client=Mock(), environ=ANTHROPIC_ENV | {"ANTHROPIC_REFUSAL_FALLBACK": fallback}
    )
    client._invoke = Mock(
        return_value=LLMResult(
            text='{"message":"ok"}',
            model_id="claude-haiku-5-5",
            input_tokens=3,
            output_tokens=2,
            usage={"requested_model": "claude-sonnet-5-5", "served_by_fallback": True},
        )
    )
    record = RecordingClient(client, SAMPLE, tmp_path)
    if fallback == "off":
        with pytest.raises(PlaybackError, match="model_mismatch"):
            record.complete("system", "user", tier="strong", schema=Message, stage="transform")
        return
    record.complete("system", "user", tier="strong", schema=Message, stage="transform")
    replay = ReplayClient(SAMPLE, tmp_path, provider="anthropic")
    result = replay.complete("system", "user", tier="strong", schema=Message, stage="transform")
    assert result.model_id == "replay:claude-haiku-5-5"
    replay.assert_consumed()


def test_replay_hash_failure_is_not_swallowed_by_pipeline(tmp_path):
    record_anthropic(tmp_path)
    replay = ReplayClient(SAMPLE, tmp_path, provider="anthropic")
    with pytest.raises(PlaybackError, match="prompt_hash_mismatch"):
        run_analysis(SAMPLE, out_dir=tmp_path / "out", llm=replay, log=lambda *_: None)


def test_fake_recordings_cannot_be_replayed_as_real_provider(tmp_path):
    record = RecordingClient(FakeLLMClient(['{"message":"ok"}']), SAMPLE, tmp_path)
    record.complete("system", "user", tier="fast", schema=Message, stage="diagnose")
    for provider in ("anthropic", "bedrock"):
        with pytest.raises(PlaybackError, match="fixture_missing"):
            ReplayClient(SAMPLE, tmp_path, provider=provider)


def test_two_providers_do_not_overwrite_the_same_stage(tmp_path):
    for provider in ("bedrock", "anthropic"):
        client, _ = backend(provider)
        record = RecordingClient(client, SAMPLE, tmp_path)
        record.complete("system", "user", tier="fast", schema=Message, stage="diagnose")
    bedrock = json.loads((tmp_path / "bedrock/todo/diagnose.json").read_text())
    anthropic = json.loads((tmp_path / "anthropic/todo/diagnose.json").read_text())
    assert bedrock["origin"] == "BedrockClient" and anthropic["origin"] == "AnthropicClient"
    assert bedrock["entries"][0]["prompt_hash"] != anthropic["entries"][0]["prompt_hash"]
    for provider in ("bedrock", "anthropic"):
        replay = ReplayClient(SAMPLE, tmp_path, provider=provider)
        replay.complete("system", "user", tier="fast", schema=Message, stage="diagnose")
        replay.assert_consumed()


def test_requested_model_is_bound_to_v2_prompt_hash(tmp_path):
    record_anthropic(tmp_path)
    manifest_path = tmp_path / "anthropic/todo/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["models"]["fast"] = "claude-sonnet-5-5"
    manifest_path.write_text(json.dumps(manifest))
    path = tmp_path / "anthropic/todo/diagnose.json"
    data = json.loads(path.read_text())
    entry = data["entries"][0]
    entry["response"]["model_id"] = "claude-sonnet-5-5"
    entry["response"]["usage"]["requested_model"] = "claude-sonnet-5-5"
    entry["response_hash"] = digest(entry["response"])
    path.write_text(json.dumps(data))
    replay = ReplayClient(SAMPLE, tmp_path, provider="anthropic")
    with pytest.raises(PlaybackError, match="prompt_hash_mismatch"):
        replay.complete("system", "user", tier="fast", schema=Message, stage="diagnose")


def test_fake_replay_cannot_be_saved_as_real_provider_cache(tmp_path):
    fake = FakeLLMClient(
        {
            "diagnose": ['{"explanations":[],"candidates":[]}'],
            "recommend": [recommendation_response],
        }
    )
    result = run_analysis(
        SAMPLE, out_dir=tmp_path / "out", llm=fake, decision_llm=fake, log=lambda *_: None
    )
    try:
        # 메모리 안의 합성 필드로 보호 장치를 확인한다. 가짜 결과를 증거로 쓰지 않는다.
        result.execution_source = "llm_replay"
        result.gate_report.status = "passed"
        result.gate_report.runner = "docker"
        for call in result.cost.calls:
            call.model_id = "replay:" + call.model_id
        with pytest.raises(ValueError, match="actual_provider_and_docker"):
            save_cache(
                SAMPLE,
                result,
                [],
                root=tmp_path / "cache",
                allow_replay=True,
                recorded_llm_usage={"provider": "bedrock", "calls": 2},
            )
        assert not (tmp_path / "cache").exists()
    finally:
        result.build_context.cleanup()


def test_cache_builder_uses_provider_and_matching_demo_environment(tmp_path, monkeypatch):
    path = ROOT / "ai/scripts/build_demo_cache.py"
    spec = importlib.util.spec_from_file_location("cache_builder_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = SimpleNamespace(build_context=None)
    run = Mock(return_value=result)
    save = Mock()
    monkeypatch.setattr(module, "run_analysis", run)
    monkeypatch.setattr(module, "save_cache", save)
    monkeypatch.setattr(module, "RecordingClient", Mock())
    monkeypatch.setattr(module, "AnthropicClient", Mock())
    monkeypatch.setattr(module, "BedrockClient", Mock(side_effect=AssertionError("wrong provider")))
    monkeypatch.setattr(module, "DockerCliRunner", Mock())
    monkeypatch.setattr(
        "sys.argv", ["build_demo_cache", "--llm", "anthropic", "--fixtures", str(tmp_path)]
    )
    assert module.main() == 0
    for name, call, saved in zip(
        ("todo", "todo-scheduler"), run.call_args_list, save.call_args_list, strict=True
    ):
        env = DEMO_ENVIRONMENTS[name]
        assert call.kwargs["target_env"] == env
        assert saved.kwargs["settings"] == settings_key(name, target_env=env)
        assert saved.kwargs["provider"] == "anthropic"

"""소유한 샘플에만 쓰는 선택적 전송 녹화와 엄격한 오프라인 재생."""

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from pydantic import Field

from ai.detectors import RepoView
from ai.llm.anthropic import MODELS, AnthropicClient
from ai.llm.base import LLMResult, Tier, ValidatingClient
from ai.llm.bedrock import BedrockClient
from ai.llm.cost import CostTracker, ModelPricing
from ai.llm.fake import FakeLLMClient
from ai.models import OutputModel
from ai.security import SourceMasker
from ai.transform.service import identify_sample

STAGES = {"diagnose", "transform", "dockerfile", "spec", "recommend", "repair"}
DEFAULT_FIXTURES = Path(__file__).resolve().parents[3] / "tests/fixtures/llm"
Provider = Literal["bedrock", "anthropic", "fake"]
ORIGINS = {"bedrock": "BedrockClient", "anthropic": "AnthropicClient", "fake": "FakeLLMClient"}
LEGACY_MODELS = {"fast": "global.anthropic.claude-haiku-4-5-20251001-v1:0"}
LEGACY_PARAMETERS = {"fast": {"inferenceConfig": {"maxTokens": 4096, "temperature": 0}}}


class PlaybackError(Exception):
    """치명적인 재생 오류. 일반 LLM 대체 처리에서 삼킬 수 없다."""


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def prompt_digest(system: str, user: str, tier: str, parameters: dict) -> str:
    return digest({"system": system, "user": user, "tier": tier, "parameters": parameters})


def assert_public(text: str) -> None:
    patterns = (
        r"(?<!\d)(?!0{12}(?!\d)|1{12}(?!\d))\d{12}(?!\d)",
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>/@]+:[^\s\"'<>/@]+@",
        r"/(?:Users|home)/[^/\s]+/",
        r"(?i)(?:password|secret[_-]?key|api[_-]?key|access[_-]?token)[\"']?\s*[:=]\s*"
        r"[\"'](?!dummy-secret-do-not-use|\[REDACTED\])[^\"']+",
    )
    # 이미지·tree SHA 문자열에는 숫자 12개가 연속으로 나올 수 있다. 계정 ID가 아니다.
    without_hashes = re.sub(r"\b[a-f0-9]{24,64}\b", "[HASH]", text)
    if re.search(patterns[0], without_hashes) or any(
        re.search(pattern, text) for pattern in patterns[1:]
    ):
        raise PlaybackError("fixture_sensitive_material: 원문을 저장하거나 출력하지 않습니다.")


def write_json(path: Path, value: dict) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    assert_public(text)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise PlaybackError("fixture_symlink_forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(text)
            file.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class Entry(OutputModel):
    tier: Literal["strong", "fast"]
    prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    response: dict
    request_parameters: dict | None = None


class Fixture(OutputModel):
    format_version: Literal[1, 2] = 2
    sample: Literal["todo", "todo-scheduler"]
    stage: str
    origin: str
    parameters: dict
    entries: list[Entry] = Field(min_length=1, max_length=50)


class RecordingManifest(OutputModel):
    format_version: Literal[2] = 2
    sample: Literal["todo", "todo-scheduler"]
    provider: Provider
    origin: str
    models: dict[Tier, str]
    parameters: dict[Tier, dict]
    legacy: bool = False


def _read_json(path: Path) -> str:
    if (
        path.is_symlink()
        or any(p.is_symlink() for p in path.parents)
        or path.stat().st_size > 524288
    ):
        raise PlaybackError("fixture_file_invalid")
    text = path.read_text(encoding="utf-8")
    assert_public(text)
    return text


def _provider(backend: ValidatingClient) -> Provider:
    for name, kind in (
        ("bedrock", BedrockClient),
        ("anthropic", AnthropicClient),
        ("fake", FakeLLMClient),
    ):
        if isinstance(backend, kind):
            return name
    raise PlaybackError("recording_provider_unknown")


def _model_matches(provider: Provider, model: str, tier: Tier) -> bool:
    if provider == "anthropic":
        return model in MODELS
    if provider == "fake":
        return model == f"fake-{tier}"
    return model.startswith(
        (
            "global.",
            "us.",
            "eu.",
            "apac.",
            "anthropic.",
            "amazon.",
            "meta.",
            "mistral.",
            "cohere.",
            "ai21.",
            "deepseek.",
            "openai.",
        )
    )


def _validate_manifest(manifest: RecordingManifest, sample: str, provider: Provider) -> None:
    if (
        manifest.sample != sample
        or manifest.provider != provider
        or manifest.origin != ORIGINS[provider]
    ):
        raise PlaybackError("fixture_provider_or_identity_mismatch")
    if not manifest.models or set(manifest.models) != set(manifest.parameters):
        raise PlaybackError("fixture_parameters_mismatch")
    if manifest.legacy and provider != "bedrock":
        raise PlaybackError("fixture_legacy_provider_mismatch")
    if manifest.legacy and manifest.models != LEGACY_MODELS:
        raise PlaybackError("fixture_model_mismatch")
    if manifest.legacy and manifest.parameters != LEGACY_PARAMETERS:
        raise PlaybackError("fixture_parameters_mismatch")
    for tier, model in manifest.models.items():
        if not _model_matches(provider, model, tier):
            raise PlaybackError("fixture_model_mismatch")


def _validate_response(provider: Provider, model: str, parameters: dict, result: LLMResult) -> None:
    if result.model_id == model:
        if provider == "anthropic" and result.usage.get("requested_model", model) != model:
            raise PlaybackError("fixture_model_mismatch")
        return
    # 녹화된 서버 측 대체 응답은 다른 모델이 응답한 것일 뿐 새 요청이 아니다.
    if (
        provider == "anthropic"
        and parameters.get("fallbacks") == "default"
        and result.model_id in MODELS
        and result.usage.get("requested_model") == model
        and result.usage.get("served_by_fallback") is True
    ):
        return
    raise PlaybackError("fixture_model_mismatch")


def _request_identity(provider: Provider, model: str, parameters: dict) -> dict:
    return {"provider": provider, "requested_model": model, "parameters": parameters}


def sample_inputs(repo: str | Path, fixture_root: Path) -> tuple[str, SourceMasker]:
    view = RepoView(repo)
    sample = identify_sample(view)
    masker = SourceMasker(view)
    if sample is None or view.warnings or masker.blocked_files:
        raise PlaybackError("record/replay는 해시로 확인한 두 자체 샘플만 지원합니다.")
    source = view.root
    target = fixture_root.resolve()
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise PlaybackError("원본 보호: fixture는 입력 레포와 분리하세요.")
    return sample, masker


class RecordingClient(ValidatingClient):
    def __init__(self, backend: ValidatingClient, repo: str | Path, root: Path = DEFAULT_FIXTURES):
        super().__init__(tracker=CostTracker(backend.tracker.prices))
        self.sample, self.masker = sample_inputs(repo, root)
        self.backend = backend
        self.root = root
        self.provider = _provider(backend)
        self.models = dict(getattr(backend, "models", {})) or {
            tier: f"fake-{tier}" for tier in ("strong", "fast")
        }
        self.parameters = {tier: deepcopy(backend.request_parameters(tier)) for tier in self.models}
        self.manifest = RecordingManifest(
            sample=self.sample,
            provider=self.provider,
            origin=ORIGINS[self.provider],
            models=self.models,
            parameters=self.parameters,
        )
        _validate_manifest(self.manifest, self.sample, self.provider)
        self.directory = root / self.provider / self.sample
        path = self.directory / "manifest.json"
        try:
            if (
                path.exists()
                and RecordingManifest.model_validate_json(_read_json(path)) != self.manifest
            ):
                raise PlaybackError("recording_configuration_mismatch_use_fresh_directory")
        except (ValueError, OSError):
            raise PlaybackError("fixture_manifest_invalid") from None
        write_json(path, self.manifest.model_dump(mode="json"))
        self.recordings: dict[str, Fixture] = {}

    def request_parameters(self, tier: Tier) -> dict:
        return deepcopy(self.parameters[tier])

    def _invoke(self, system: str, user: str, *, tier: Tier, stage: str) -> LLMResult:
        if stage not in STAGES:
            raise PlaybackError("fixture_stage_invalid")
        if stage in self.recordings and len(self.recordings[stage].entries) >= 50:
            raise PlaybackError("fixture_stage_limit")
        if self.backend.request_parameters(tier) != self.parameters[tier] or (
            getattr(self.backend, "models", self.models).get(tier) != self.models[tier]
        ):
            raise PlaybackError("recording_configuration_changed")
        result = self.backend._invoke(system, user, tier=tier, stage=stage)
        _validate_response(self.provider, self.models[tier], self.parameters[tier], result)
        response = result.model_dump(mode="json", exclude={"parsed"})
        # 알려진 비밀·자격 증명 치환을 빼고는 응답 바이트를 그대로 보존한다.
        safe_text = self.masker.text(result.text, allow_dummy=True)
        safe_text = re.sub(
            r"([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@",
            r"\1[REDACTED]@",
            safe_text,
            flags=re.I,
        )
        safe_text = re.sub(r"(?<!\d)\d{12}(?!\d)", "[ACCOUNT_ID]", safe_text)
        assert_public(safe_text)
        response["text"] = safe_text
        parameters = self.request_parameters(tier)
        entry = Entry(
            tier=tier,
            prompt_hash=prompt_digest(
                system, user, tier, _request_identity(self.provider, self.models[tier], parameters)
            ),
            response_hash=digest(response),
            response=response,
            request_parameters=parameters,
        )
        if stage not in self.recordings:
            self.recordings[stage] = Fixture(
                sample=self.sample,
                stage=stage,
                origin=ORIGINS[self.provider],
                parameters=parameters,
                entries=[entry],
            )
        else:
            self.recordings[stage].entries.append(entry)
        fixture = self.recordings[stage]
        write_json(self.directory / f"{stage}.json", fixture.model_dump(mode="json"))
        return result.model_copy(update={"text": safe_text})


class ReplayClient(ValidatingClient):
    is_replay = True

    def __init__(
        self,
        repo: str | Path,
        root: Path = DEFAULT_FIXTURES,
        *,
        provider: Provider = "bedrock",
        expected_models: dict[Tier, str] | None = None,
        expected_parameters: dict[Tier, dict] | None = None,
    ):
        super().__init__()
        if provider not in ORIGINS:
            raise PlaybackError("fixture_provider_unknown")
        self.sample, _ = sample_inputs(repo, root)
        self.fixtures = {}
        self.cursors: dict[str, int] = {}
        self.provider = provider
        directory = root / provider / self.sample
        if not directory.exists() and provider == "bedrock":
            directory = root / self.sample  # 기존의 변경 불가 v1 Bedrock 녹화본.
        try:
            if not directory.exists():
                raise PlaybackError(f"fixture_missing: {self.sample}")
            self.manifest = RecordingManifest.model_validate_json(
                _read_json(directory / "manifest.json")
            )
            _validate_manifest(self.manifest, self.sample, provider)
            if expected_models is not None and expected_models != self.manifest.models:
                raise PlaybackError("fixture_model_mismatch")
            if expected_parameters is not None and expected_parameters != self.manifest.parameters:
                raise PlaybackError("fixture_parameters_mismatch")
            self.models = dict(self.manifest.models)
            self.parameters = deepcopy(self.manifest.parameters)
            for path in sorted(directory.glob("*.json")):
                if path.name == "manifest.json":
                    continue
                if path.stem not in STAGES or path.is_symlink() or path.stat().st_size > 524288:
                    raise PlaybackError("fixture_file_invalid")
                if any(parent.is_symlink() for parent in path.parents):
                    raise PlaybackError("fixture_symlink_forbidden")
                text = _read_json(path)
                fixture = Fixture.model_validate_json(text)
                if fixture.sample != self.sample or fixture.stage != path.stem:
                    raise PlaybackError("fixture_identity_mismatch")
                if fixture.origin != ORIGINS[provider] or fixture.format_version != (
                    1 if self.manifest.legacy else 2
                ):
                    raise PlaybackError("fixture_provider_mismatch")
                if fixture.parameters != self.parameters.get(fixture.entries[0].tier):
                    raise PlaybackError("fixture_parameters_mismatch")
                for entry in fixture.entries:
                    parameters = self.parameters.get(entry.tier)
                    if parameters is None or (
                        not self.manifest.legacy and entry.request_parameters != parameters
                    ):
                        raise PlaybackError("fixture_parameters_mismatch")
                    if digest(entry.response) != entry.response_hash:
                        raise PlaybackError("fixture_response_hash_mismatch")
                    result = LLMResult.model_validate(entry.response)
                    _validate_response(provider, self.models[entry.tier], parameters, result)
                    self.tracker.prices["replay:" + result.model_id] = ModelPricing(
                        input_usd_per_million=0, output_usd_per_million=0
                    )
                self.fixtures[fixture.stage] = fixture
        except (ValueError, OSError) as error:
            raise PlaybackError(f"fixture_read_invalid: {type(error).__name__}") from None
        if not self.fixtures:
            raise PlaybackError(f"fixture_missing: {self.sample}")

    def request_parameters(self, tier: Tier) -> dict:
        return self.parameters.get(tier, {})

    def _invoke(self, system: str, user: str, *, tier: Tier, stage: str) -> LLMResult:
        fixture = self.fixtures.get(stage)
        index = self.cursors.get(stage, 0)
        if fixture is None or index >= len(fixture.entries):
            raise PlaybackError(f"fixture_request_missing: {self.sample}/{stage} #{index + 1}")
        entry = fixture.entries[index]
        parameters = self.request_parameters(tier)
        identity = (
            parameters
            if self.manifest.legacy
            else _request_identity(self.provider, self.models[tier], parameters)
        )
        actual = prompt_digest(system, user, tier, identity)
        if tier != entry.tier or actual != entry.prompt_hash:
            raise PlaybackError(
                f"prompt_hash_mismatch: {self.sample}/{stage} #{index + 1}; "
                "프롬프트/스키마/입력이 바뀌었습니다. refresh_golden.py로 의도한 변경을 검토하세요."
            )
        self.cursors[stage] = index + 1
        recorded = LLMResult.model_validate(entry.response)
        return LLMResult(
            text=recorded.text,
            input_tokens=0,
            output_tokens=0,
            model_id="replay:" + recorded.model_id,
            transport_attempts=0,
            stop_reason=recorded.stop_reason,
            usage={"source": "llm_replay", "recorded_usage": recorded.usage},
        )

    def assert_consumed(self) -> None:
        if any(self.cursors.get(stage, 0) != len(f.entries) for stage, f in self.fixtures.items()):
            raise PlaybackError("fixture_requests_unused: 기록 당시와 실행 단계가 다릅니다.")

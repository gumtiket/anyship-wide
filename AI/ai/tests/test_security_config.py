import pytest

from ai.detectors import RepoView
from ai.security import SourceMasker


@pytest.mark.parametrize("flags", ["false", "true", "FALSE", "True", "false # public value"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_deploy_environment_secret_boolean_is_metadata(tmp_path, flags, newline):
    config = f"env:\n  - name: LOG_LEVEL\n    secret: {flags}\n"
    (tmp_path / "deploy-spec.yaml").write_bytes(config.replace("\n", newline).encode("utf-8"))
    masker = SourceMasker(RepoView(tmp_path))
    diff = f"+websocket: false\n+secret: true\n+secret: {flags}\n"
    assert not masker.contains_sensitive(diff)
    assert masker.text(diff) == diff
    # 기존 파일 단위 정책은 유지하고, 플래그 값만 예외로 둔다.
    assert "deploy-spec.yaml" in masker.blocked_files


@pytest.mark.parametrize(
    "name,config,secret",
    [
        ("config.yaml", "secret: false\n", "false"),
        ("deploy-spec.yaml", "secret: true\n", "true"),
        ("deploy-spec.yaml", "env:\n  - name: API_KEY\n    secret: 123456\n", "123456"),
        ("deploy-spec.yaml", 'env:\n  - name: APP_NAME\n    secret: "false"\n', "false"),
        ("deploy-spec.yaml", "env:\n  - name: APP_NAME\n    secret: 'true'\n", "true"),
        ("deploy-spec.yaml", "env:\n  - name: API_KEY\n    api_key: 123456\n", "123456"),
        ("deploy-spec.yaml", "env:\n  - name: APP_NAME\n    password: false\n", "false"),
        ("deploy-spec.yaml", "env:\n  - name: APP_NAME\n    api_secret: true\n", "true"),
        ("deploy-spec.yaml", "env:\n  - name: APP_NAME\n    secret: [\n", "["),
        (
            "deploy-spec.yaml",
            "env:\n  - name: APP_NAME\n    secret: false\npassword: false\n",
            "false",
        ),
        (
            "deploy-spec.yaml",
            "env:\n  - name: APP_NAME\n    secret: dummy-test-password-not-real\n",
            "dummy-test-password-not-real",
        ),
    ],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_real_config_credentials_still_block_and_mask(tmp_path, name, config, secret, newline):
    (tmp_path / name).write_bytes(config.replace("\n", newline).encode("utf-8"))
    masker = SourceMasker(RepoView(tmp_path))
    assert masker.contains_sensitive(secret)
    assert masker.text(secret) == "[REDACTED]"
    assert name in masker.blocked_files


def test_yaml_alias_does_not_exempt_a_credential_at_its_anchor(tmp_path):
    (tmp_path / "deploy-spec.yaml").write_bytes(
        b"defaults:\n  secret: &flag false\nenv:\n  - name: APP_NAME\n    secret: *flag\n",
    )
    masker = SourceMasker(RepoView(tmp_path))
    assert masker.contains_sensitive("&flag false")
    assert masker.contains_sensitive("*flag")

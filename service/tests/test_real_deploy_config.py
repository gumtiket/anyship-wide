from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.config import Settings

KEY = Fernet.generate_key().decode()
REAL = dict(
    deployment_mode="real",
    token_key=KEY,
    deploy_source_dir=Path("/srv/sources"),
    deploy_ssh_key=Path("/home/ec2-user/.ssh/deploy"),
    deploy_service_ip="203.0.113.10",
    deploy_acme_email="ops@example.com",
    deploy_base_domain="anyship.cloud",
)
PRODUCTION = dict(
    production=True,
    app_origin="https://app.anyship.cloud",
    database_url="postgresql+psycopg://anyship@127.0.0.1:5432/anyship",
    github_client_id="id",
    github_client_secret="secret",
    github_app_slug="anyship",
)


def real(**override):
    return Settings(**{**REAL, **override})


def test_the_default_and_the_mock_mode_need_no_deployment_settings():
    assert Settings(demo=True).deployment_mode == "unavailable"
    assert Settings(demo=True, deployment_mode="mock").deployment_mode == "mock"


def test_real_mode_accepts_complete_settings_and_keeps_the_defaults_for_the_rest():
    settings = real()
    assert (
        settings.deployment_mode == "real"
        and settings.deploy_verify_tls is True
        and settings.deploy_dns is True
    )
    assert settings.deploy_terraform_dir.parts[-2:] == ("infra", "user-account")
    assert settings.deploy_plugin_cache.parts[-2:] == (".terraform.d", "plugin-cache")


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="unavailable, mock or real"):
        Settings(demo=True, deployment_mode="live")


def test_real_mode_cannot_run_in_demo_mode():
    with pytest.raises(ValueError, match="APP_DEMO"):
        real(demo=True)


@pytest.mark.parametrize(
    "field, variable",
    [
        ("deploy_ssh_key", "APP_DEPLOY_SSH_KEY"),
        ("deploy_service_ip", "APP_DEPLOY_SERVICE_IP"),
        ("deploy_acme_email", "APP_DEPLOY_ACME_EMAIL"),
        ("deploy_base_domain", "APP_DEPLOY_BASE_DOMAIN"),
    ],
)
def test_a_missing_setting_is_named_at_startup(field, variable):
    empty = None if field == "deploy_ssh_key" else ""
    with pytest.raises(ValueError) as caught:
        real(**{field: empty})
    assert variable in str(caught.value)


def test_every_missing_setting_is_listed_at_once():
    with pytest.raises(ValueError) as caught:
        Settings(deployment_mode="real", token_key=KEY)
    assert all(
        name in str(caught.value)
        for name in (
            "APP_DEPLOY_SSH_KEY",
            "APP_DEPLOY_SERVICE_IP",
            "APP_DEPLOY_ACME_EMAIL",
            "APP_DEPLOY_BASE_DOMAIN",
        )
    )
    assert "APP_DEPLOY_SOURCE_DIR" not in str(caught.value)  # 기본(github)에서는 폴더가 필요 없다


def test_the_source_defaults_to_github_which_needs_no_folder():
    settings = real(deploy_source_dir=None)
    assert settings.deploy_source == "github" and settings.deploy_source_dir is None
    assert settings.deploy_workspace.parts[-2:] == ("workspaces", "deploy-src")


def test_the_local_source_needs_its_folder_and_names_it_when_it_is_missing():
    assert real(deploy_source="local").deploy_source == "local"
    for missing in (None, ""):
        with pytest.raises(ValueError) as caught:
            real(deploy_source="local", deploy_source_dir=missing)
        assert "APP_DEPLOY_SOURCE_DIR" in str(caught.value)


@pytest.mark.parametrize("value", ["git", "s3", "GitHub", "", "github;local", "folder"])
def test_an_unknown_source_is_refused_by_name_without_guessing(value):
    with pytest.raises(ValueError, match="APP_DEPLOY_SOURCE must be github or local"):
        real(deploy_source=value)


@pytest.mark.parametrize(
    "override",
    [
        {"deploy_service_ip": "not-an-ip"},
        {"deploy_service_ip": "203.0.113.10; rm -rf /"},
        {"deploy_service_ip": "999.1.1.1"},
        {"deploy_acme_email": "no-at-sign"},
        {"deploy_acme_email": "a b@example.com"},
        {"deploy_acme_email": "ops@nodot"},
        {"deploy_base_domain": "Anyship.Cloud"},
        {"deploy_base_domain": "nodots"},
        {"deploy_base_domain": "-bad.example.com"},
        {"deploy_base_domain": "anyship.cloud/path"},
        {"deploy_base_domain": "a b.example.com"},
    ],
)
def test_malformed_values_are_refused_without_echoing_them(override):
    with pytest.raises(ValueError) as caught:
        real(**override)
    assert str(next(iter(override.values()))) not in str(caught.value)


def test_both_address_families_are_accepted_for_the_service_ip():
    assert real(deploy_service_ip="2001:db8::10").deploy_service_ip == "2001:db8::10"


def test_real_mode_is_allowed_in_production_but_mock_is_not():
    assert real(**PRODUCTION).production
    with pytest.raises(ValueError, match="only available in development"):
        Settings(**{**PRODUCTION, "deployment_mode": "mock", "token_key": KEY})


def test_settings_do_not_show_up_in_repr_with_secrets():
    assert KEY not in repr(real())


def test_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("APP_DEMO", "false")
    monkeypatch.setenv("APP_TOKEN_KEY", KEY)
    monkeypatch.setenv("APP_DEPLOYMENT_MODE", "real")
    monkeypatch.setenv("APP_DEPLOY_SOURCE_DIR", "~/anyship-sources")
    monkeypatch.setenv("APP_DEPLOY_SSH_KEY", "/home/ec2-user/.ssh/deploy")
    monkeypatch.setenv("APP_DEPLOY_SERVICE_IP", " 203.0.113.10 ")
    monkeypatch.setenv("APP_DEPLOY_ACME_EMAIL", "ops@example.com")
    monkeypatch.setenv("APP_DEPLOY_BASE_DOMAIN", "Anyship.Cloud")
    monkeypatch.setenv("APP_DEPLOY_VERIFY_TLS", "false")
    monkeypatch.setenv("APP_DEPLOY_TERRAFORM_DIR", "/opt/infra/user-account")
    monkeypatch.setenv("APP_DEPLOY_PLUGIN_CACHE", "/var/cache/terraform")
    settings = Settings.from_env()
    assert settings.deploy_source_dir == Path("~/anyship-sources").expanduser()
    assert settings.deploy_ssh_key == Path("/home/ec2-user/.ssh/deploy")
    assert (settings.deploy_service_ip, settings.deploy_base_domain) == (
        "203.0.113.10",
        "anyship.cloud",
    )
    assert settings.deploy_verify_tls is False
    assert settings.deploy_terraform_dir == Path("/opt/infra/user-account")
    assert settings.deploy_plugin_cache == Path("/var/cache/terraform")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("on", True),
        ("off", False),
        (" OFF ", False),
        ("On", True),
        (None, True),
        ("", True),
        ("  ", True),
    ],
)
def test_dns_management_is_read_from_the_environment_and_defaults_to_on(
    monkeypatch, value, expected
):
    monkeypatch.setenv("APP_DEMO", "true")
    if value is None:
        monkeypatch.delenv("APP_DEPLOY_DNS", raising=False)
    else:
        monkeypatch.setenv("APP_DEPLOY_DNS", value)
    assert Settings.from_env().deploy_dns is expected


@pytest.mark.parametrize("value", ["yes", "true", "0", "auto", "off;on"])
def test_an_unclear_dns_setting_stops_the_server_instead_of_guessing(monkeypatch, value):
    monkeypatch.setenv("APP_DEMO", "true")
    monkeypatch.setenv("APP_DEPLOY_DNS", value)
    with pytest.raises(ValueError, match="APP_DEPLOY_DNS"):
        Settings.from_env()


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "github"),
        ("", "github"),
        ("  ", "github"),
        ("github", "github"),
        ("LOCAL", "local"),
        (" local ", "local"),
    ],
)
def test_the_source_is_read_from_the_environment_and_defaults_to_github(
    monkeypatch, value, expected
):
    monkeypatch.setenv("APP_DEMO", "true")
    for name in ("APP_DEPLOY_SOURCE", "APP_DEPLOY_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    if value is not None:
        monkeypatch.setenv("APP_DEPLOY_SOURCE", value)
    assert Settings.from_env().deploy_source == expected


def test_an_unclear_source_in_the_environment_stops_the_server_when_real_deployment_is_on(
    monkeypatch,
):
    monkeypatch.setenv("APP_DEMO", "false")
    monkeypatch.setenv("APP_TOKEN_KEY", KEY)
    monkeypatch.setenv("APP_DEPLOYMENT_MODE", "real")
    monkeypatch.setenv("APP_DEPLOY_SSH_KEY", "/k")
    monkeypatch.setenv("APP_DEPLOY_SERVICE_IP", "203.0.113.10")
    monkeypatch.setenv("APP_DEPLOY_ACME_EMAIL", "ops@example.com")
    monkeypatch.setenv("APP_DEPLOY_BASE_DOMAIN", "anyship.cloud")
    monkeypatch.setenv("APP_DEPLOY_SOURCE", "tarball")
    with pytest.raises(ValueError, match="APP_DEPLOY_SOURCE"):
        Settings.from_env()


def test_the_workspace_for_fetched_sources_can_be_set_from_the_environment(monkeypatch):
    monkeypatch.setenv("APP_DEMO", "true")
    monkeypatch.setenv("APP_DEPLOY_WORKSPACE", "/var/lib/anyship/src")
    assert Settings.from_env().deploy_workspace == Path("/var/lib/anyship/src")


def test_environment_defaults_leave_real_deployment_off(monkeypatch):
    for name in (
        "APP_DEPLOYMENT_MODE",
        "APP_DEPLOY_SOURCE_DIR",
        "APP_DEPLOY_VERIFY_TLS",
        "APP_DEPLOY_TERRAFORM_DIR",
        "APP_DEPLOY_PLUGIN_CACHE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_DEMO", "true")
    settings = Settings.from_env()
    assert settings.deployment_mode == "unavailable" and settings.deploy_source_dir is None
    assert settings.deploy_verify_tls is True

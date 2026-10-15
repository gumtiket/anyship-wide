import pytest
from specs import GENERATED, HAND_WRITTEN, make

from anyship_adapters.spec import SpecError, parse_spec


def rejected(spec):
    with pytest.raises(SpecError) as caught:
        parse_spec(spec)
    return caught.value.error


# --- AI가 실제로 만드는 명세는 통과한다 -----------------------------------------------
def test_generated_spec_is_accepted_and_port_in_env_is_skipped_with_a_warning():
    parsed = parse_spec(GENERATED)
    assert parsed.app == "todo" and parsed.port == 8080 and parsed.postgres
    assert [v.name for v in parsed.env] == ["LOG_LEVEL", "SECRET_KEY"]
    assert any("PORT" in w for w in parsed.warnings)
    assert parsed.migrate == "python -m app.migrate"


def test_hand_written_spec_is_accepted_and_object_storage_is_skipped_with_a_warning():
    parsed = parse_spec(HAND_WRITTEN)
    assert parsed.postgres
    assert any("object_storage" in w for w in parsed.warnings)


def test_a_spec_without_a_database_or_release_is_fine():
    parsed = parse_spec(make(backing_services=[], release=None))
    assert not parsed.postgres and parsed.migrate is None


def test_unused_fields_are_ignored():
    assert (
        parse_spec(make(profile="prod", source={"repo": "x"}, workload={"anything": 1})).app
        == "todo"
    )


# --- 앱 이름, 포트, 경로 ----------------------------------------------------------------
@pytest.mark.parametrize("name", ["Todo", "to", "1todo", "todo_app", "todo app", "a" * 64, ""])
def test_bad_app_names_are_rejected(name):
    error = rejected(make(app=name))
    assert error.code == "invalid_spec" and "app" in error.message


@pytest.mark.parametrize("port", [80, 1023, 70000, "abc"])
def test_bad_ports_are_rejected(port):
    assert rejected(make(port=port)).code == "invalid_spec"


@pytest.mark.parametrize("path", ["healthz", "/health z", "/a;b", "/" + "a" * 300])
def test_bad_health_check_paths_are_rejected(path):
    assert rejected(make(healthcheck=path)).code == "invalid_spec"


def test_only_one_web_instance_is_supported():
    assert rejected(make(processes={"web": {"instances": 2}})).code == "invalid_spec"


def test_non_mapping_specs_are_rejected():
    assert rejected("todo").code == "invalid_spec"
    assert rejected(None).code == "invalid_spec"


def test_error_messages_name_the_field_but_never_echo_values():
    spec = make(env=[{"name": "TOKEN", "secret": False, "value": "hunter2\nhunter2"}])
    error = rejected(spec)
    assert "hunter2" not in error.message + (error.hint or "")


# --- 환경변수 -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "LD_PRELOAD",
        "AWS_ACCESS_KEY_ID",
        "AWS_SESSION_TOKEN",
        "DOCKER_HOST",
        "PATH",
        "HOME",
        "DATABASE_URL",
        "STORAGE_URL",
        "POSTGRES_PASSWORD",
        "COMPOSE_FILE",
        "TRAEFIK_API",
    ],
)
def test_reserved_env_names_are_rejected(name):
    error = rejected(make(env=[{"name": name, "value": "x"}]))
    assert error.code == "invalid_env_name" and name in error.message


@pytest.mark.parametrize("name", ["lower", "1ABC", "A-B", "A B", "", "A" * 65])
def test_malformed_env_names_are_rejected(name):
    assert rejected(make(env=[{"name": name, "value": "x"}])).code == "invalid_env_name"


def test_duplicate_env_names_are_rejected():
    assert (
        rejected(make(env=[{"name": "A", "value": "1"}, {"name": "A", "value": "2"}])).code
        == "invalid_env_name"
    )


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\0b", "it's", "x" * 1025])
def test_env_values_that_cannot_be_written_safely_are_rejected(value):
    assert rejected(make(env=[{"name": "A", "value": value}])).code == "invalid_env_value"


def test_port_env_with_a_different_value_is_rejected():
    assert rejected(make(env=[{"name": "PORT", "value": "9999"}])).code == "invalid_env_name"


def test_secret_rules():
    assert (
        rejected(make(env=[{"name": "A", "secret": True, "value": "x"}])).code
        == "invalid_secret_config"
    )
    assert (
        rejected(make(env=[{"name": "A", "secret": False, "generate": True}])).code
        == "invalid_secret_config"
    )
    assert rejected(make(env=[{"name": "A", "secret": False}])).code == "invalid_secret_config"
    # 사용자가 입력할 비밀(secret만 true)과 생성할 비밀(secret과 generate)은 통과한다.
    assert parse_spec(
        make(env=[{"name": "A", "secret": True}, {"name": "B", "secret": True, "generate": True}])
    )


# --- 외부 자원과 마이그레이션 ----------------------------------------------------------------
def test_unknown_backing_services_are_rejected():
    error = rejected(make(backing_services=[{"type": "redis", "bind_as": "REDIS_URL"}]))
    assert error.code == "unsupported_backing_service"


def test_postgres_must_bind_to_database_url_and_appear_once():
    assert (
        rejected(make(backing_services=[{"type": "postgres", "bind_as": "OTHER"}])).code
        == "invalid_spec"
    )
    twice = [{"type": "postgres", "bind_as": "DATABASE_URL"}] * 2
    assert rejected(make(backing_services=twice)).code == "invalid_spec"


@pytest.mark.parametrize("command", ["", "  ", "a\nb", "x" * 501])
def test_bad_migrate_commands_are_rejected(command):
    assert rejected(make(release={"migrate": command})).code == "invalid_spec"

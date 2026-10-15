import re

import pytest
import yaml
from specs import GENERATED, HAND_WRITTEN, make

from anyship_adapters.compose import RenderedStack, render_stack
from anyship_adapters.spec import SpecError

TAG = "3f2a9c1"
HOST = "todo.demo.onprem.anyship.cloud"  # 주소는 어댑터가 정해서 넘긴다


def counter(prefix="tok"):
    """예측 가능한 토큰을 만드는 함수(영문자와 숫자만)."""
    state = {"n": 0}

    def make_token():
        state["n"] += 1
        return f"{prefix}{state['n']:030d}"

    return make_token


def render(spec=GENERATED, **kw):
    args = dict(host=HOST, image_tag=TAG, new_token=counter())
    args.update(kw)
    return render_stack(spec, **args)


def compose(stack: RenderedStack):
    return yaml.safe_load(stack.compose_yaml)


def rejected(spec=GENERATED, **kw):
    with pytest.raises(SpecError) as caught:
        render(spec, **kw)
    return caught.value.error


# --- 만들어지는 Compose -----------------------------------------------------------------
def test_stack_has_a_web_and_a_db_service_with_the_right_image_and_host():
    data = compose(render())
    assert set(data["services"]) == {"web", "db"}
    assert data["name"] == "todo"
    assert data["services"]["web"]["image"] == f"todo:{TAG}"
    assert f"Host(`{HOST}`)" in " ".join(data["services"]["web"]["labels"])


def test_the_host_comes_from_the_caller_so_other_environments_can_reuse_this():
    aws_host = "todo.demo.aws.anyship.cloud"
    labels = " ".join(compose(render(host=aws_host))["services"]["web"]["labels"])
    assert f"Host(`{aws_host}`)" in labels and "onprem" not in labels


def test_traefik_labels_route_https_to_the_app_port_with_a_certificate():
    labels = compose(render())["services"]["web"]["labels"]
    assert "traefik.enable=true" in labels
    assert "traefik.http.routers.todo.entrypoints=websecure" in labels
    assert "traefik.http.routers.todo.tls.certresolver=le" in labels
    assert "traefik.http.services.todo.loadbalancer.server.port=8080" in labels


def test_nothing_is_published_to_the_host_and_the_db_is_only_on_the_internal_network():
    data = compose(render())
    assert "ports" not in data["services"]["web"] and "ports" not in data["services"]["db"]
    assert data["services"]["db"]["networks"] == ["internal"]
    assert set(data["services"]["web"]["networks"]) == {"traefik", "internal"}
    assert data["networks"]["traefik"] == {"external": True}
    assert data["networks"]["internal"] == {"internal": True}


def test_web_container_is_locked_down():
    web = compose(render())["services"]["web"]
    assert web["read_only"] is True and web["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in web["security_opt"]
    assert web["tmpfs"] == ["/tmp"] and web["mem_limit"] and web["pids_limit"]


def test_app_waits_for_a_healthy_database_and_gets_its_address_from_compose():
    data = compose(render())
    assert data["services"]["web"]["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert (
        data["services"]["web"]["environment"]["DATABASE_URL"]
        == "postgresql://app:${POSTGRES_PASSWORD}@db:5432/app"
    )
    assert data["services"]["web"]["environment"]["PORT"] == "8080"
    assert data["services"]["db"]["healthcheck"]["test"][0] == "CMD-SHELL"
    assert "db-data" in data["volumes"]


def test_a_spec_without_postgres_gets_no_db_service_volume_or_database_url():
    stack = render(make(backing_services=[]))
    data = compose(stack)
    assert set(data["services"]) == {"web"}
    assert "volumes" not in data and "depends_on" not in data["services"]["web"]
    assert "DATABASE_URL" not in data["services"]["web"]["environment"]
    assert stack.compose_env == ""


def test_the_hand_written_spec_renders_and_reports_object_storage_as_skipped():
    stack = render(HAND_WRITTEN)
    assert any("object_storage" in w for w in stack.warnings)
    assert "STORAGE_URL" not in stack.compose_yaml + stack.app_env


# --- 환경 파일과 비밀 ----------------------------------------------------------------------
def test_app_env_has_the_plain_settings_and_the_generated_secret_but_not_the_db_password():
    stack = render()
    lines = stack.app_env.splitlines()
    assert "LOG_LEVEL='INFO'" in lines
    assert any(line.startswith("SECRET_KEY='") for line in lines)
    assert not any(line.startswith("PORT=") for line in lines)  # port 필드에서 주입한다
    assert "POSTGRES_PASSWORD" not in stack.app_env
    assert stack.compose_env.startswith("POSTGRES_PASSWORD='") and stack.compose_env.endswith("'\n")


def test_generated_values_are_reported_by_name_only():
    stack = render()
    assert set(stack.generated) == {"SECRET_KEY", "POSTGRES_PASSWORD"}


def test_generated_secrets_are_reused_when_the_server_already_has_them():
    first = render()
    previous = {}
    for text in (first.compose_env, first.app_env):
        for line in text.splitlines():
            name, _, value = line.partition("=")
            previous[name] = value.strip("'")
    second = render(previous_env=previous, new_token=counter("zzz"))
    assert second.generated == ()
    assert second.app_env == first.app_env and second.compose_env == first.compose_env


def test_generated_passwords_are_url_safe():
    stack = render(new_token=lambda: "a1B2" * 12)
    assert re.fullmatch(r"POSTGRES_PASSWORD='[A-Za-z0-9]+'\n", stack.compose_env)


def test_user_provided_secrets_are_written_to_app_env_only():
    spec = make(env=[{"name": "API_KEY", "secret": True}])
    stack = render(spec, secrets={"API_KEY": "s3cr3t-value-123"})
    assert "API_KEY='s3cr3t-value-123'" in stack.app_env
    assert "s3cr3t-value-123" not in stack.compose_yaml + stack.compose_env


def test_compose_yaml_never_contains_a_secret_value():
    stack = render(
        make(
            env=[
                {"name": "API_KEY", "secret": True},
                {"name": "SECRET_KEY", "secret": True, "generate": True},
            ]
        ),
        secrets={"API_KEY": "s3cr3t-value-123"},
    )
    secret_key = next(
        line for line in stack.app_env.splitlines() if line.startswith("SECRET_KEY")
    ).split("'")[1]
    password = stack.compose_env.split("'")[1]
    for secret in ("s3cr3t-value-123", secret_key, password):
        assert secret not in stack.compose_yaml


def test_missing_user_secret_is_reported_by_name():
    error = rejected(
        make(env=[{"name": "API_KEY", "secret": True}, {"name": "OTHER", "secret": True}])
    )
    assert (
        error.code == "missing_secret" and "API_KEY" in error.message and "OTHER" in error.message
    )


def test_undeclared_secret_inputs_are_ignored_with_a_warning_that_has_no_values():
    stack = render(secrets={"TYPO_KEY": "s3cr3t-value-123"})
    assert any("TYPO_KEY" in w for w in stack.warnings)
    assert "s3cr3t-value-123" not in " ".join(stack.warnings) + stack.app_env


def test_secret_values_that_cannot_be_written_safely_are_rejected():
    spec = make(env=[{"name": "API_KEY", "secret": True}])
    assert rejected(spec, secrets={"API_KEY": "line1\nINJECTED=1"}).code == "invalid_env_value"
    assert rejected(spec, secrets={"API_KEY": "it's"}).code == "invalid_env_value"


def test_a_tampered_db_password_on_the_server_is_refused():
    assert rejected(previous_env={"POSTGRES_PASSWORD": "x'; DROP"}).code == "invalid_env_value"


# --- 입력 검증 -------------------------------------------------------------------------------
@pytest.mark.parametrize("tag", ["latest", "3f2a9c", "3F2A9C1", "3f2a9c1; echo", "x" * 41, ""])
def test_image_tag_must_be_a_commit_sha(tag):
    assert rejected(image_tag=tag).code == "invalid_image_tag"


@pytest.mark.parametrize(
    "host",
    [
        "",
        "todo",
        "Todo.example.com",
        "todo..example.com",
        "-a.example.com",
        "a b.example.com",
        "todo.example.com/x",
        "a`.example.com",
        "todo.example.com:8080",
        "a" * 64 + ".example.com",
        ("a." * 130) + "com",
    ],
)
def test_host_must_be_a_plain_lowercase_domain_name(host):
    assert rejected(host=host).code == "invalid_host"


def test_spec_errors_pass_through():
    assert rejected(make(app="Bad Name")).code == "invalid_spec"


# --- 외부 DB(EC2 세트의 공유 RDS) -----------------------------------------------------------------
DB_URL = (
    "postgresql://app_todo:a1b2c3d4e5f6a7b8c9d0e1f2@anyship-test-db.abc.ap-northeast-2.rds.amazonaws.com"
    ":5432/app_todo?sslmode=require"
)


def test_an_external_database_replaces_the_db_container_and_the_closed_network():
    stack = render(database_url=DB_URL)
    config = compose(stack)
    assert "db" not in config["services"] and "volumes" not in config
    # 앱이 VPC의 RDS에 닿아야 하므로 인터넷이 막힌 앱 전용 네트워크를 만들지 않는다.
    assert config["services"]["web"]["networks"] == ["traefik"] and list(config["networks"]) == [
        "traefik"
    ]
    assert "depends_on" not in config["services"]["web"]
    assert stack.compose_env == "" and stack.generated == (
        "SECRET_KEY",
    )  # DB 비밀번호는 만들지 않는다


def test_the_external_database_address_is_written_only_to_the_secret_env_file():
    stack = render(database_url=DB_URL)
    assert f"DATABASE_URL='{DB_URL}'" in stack.app_env.splitlines()
    assert (
        "rds.amazonaws.com" not in stack.compose_yaml and "DATABASE_URL" not in stack.compose_yaml
    )


def test_without_an_external_database_the_db_container_is_still_created():
    config = compose(render())  # 온프레미스의 기존 동작이 바뀌지 않았다는 보호 장치
    assert "db" in config["services"] and "internal" in config["networks"]
    assert config["services"]["web"]["networks"] == ["traefik", "internal"]


@pytest.mark.parametrize(
    "url",
    [
        DB_URL.replace(":5432/", ":99999999/"),  # 포트가 숫자 4~5자리가 아님
        DB_URL.replace("?sslmode=require", ""),  # SSL 필수가 빠짐
        DB_URL.replace("?sslmode=require", "?sslmode=prefer"),
        DB_URL.replace("postgresql://", "mysql://"),
        DB_URL.replace("a1b2c3d4e5f6a7b8c9d0e1f2", "short"),
        DB_URL.replace("app_todo:", "APP:"),  # 대문자 계정
        DB_URL + "'; echo hacked",  # 환경 파일의 따옴표를 닫으려는 시도
        DB_URL + "\nSECRET_KEY=x",  # 줄을 바꿔 다른 변수를 끼워 넣으려는 시도
    ],
)
def test_a_malformed_external_database_address_is_refused(url):
    assert rejected(database_url=url).code == "invalid_env_value"


def test_an_external_database_address_is_ignored_with_a_warning_when_the_spec_has_no_postgres():
    stack = render(make(backing_services=[]), database_url=DB_URL)
    assert "DATABASE_URL" not in stack.app_env and DB_URL not in stack.compose_yaml
    assert any("postgres가 없어" in warning for warning in stack.warnings)
    assert "internal" in compose(stack)["networks"]  # DB가 없으면 기존처럼 앱 전용 네트워크를 둔다

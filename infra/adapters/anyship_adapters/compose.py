"""검증된 배포 명세를 앱 하나분의 Compose 파일과 환경 파일로 바꾼다.

Compose를 쓰는 세트(온프레미스, aws-always-on)가 함께 쓰는 모듈이다. 서버에 접속하지
않는 순수 함수라서, 서버 없이 테스트할 수 있다.

환경마다 다른 것은 호출하는 어댑터가 정해서 넘긴다. 예를 들어 앱의 공개 주소(host)는
온프레미스는 `<앱>.<환경ID>.onprem.<도메인>`, AWS는 `.aws.`로 어댑터가 만든다.

앱 하나 = Compose 스택 하나:
  web  앱 컨테이너. Traefik 네트워크와 앱 전용 내부 네트워크에 연결된다.
  db   Postgres 컨테이너(명세가 postgres를 요구할 때만). 앱 전용 내부 네트워크에만
       연결되고 호스트 포트로 열지 않는다. 데이터는 앱별 볼륨에 둔다.
       외부 DB(EC2 세트의 공유 RDS)를 쓰면 db 컨테이너를 만들지 않는다. 호출하는 쪽이
       `database_url`을 넘기고, 이 주소는 app.env에만 들어간다.

파일은 두 개로 나눈다. 같은 파일에 두면 DB 비밀번호가 앱의 환경변수로 새어 들어간다.
  .env      Compose 치환용(POSTGRES_PASSWORD)
  app.env   앱 컨테이너의 환경변수(일반 설정, 생성한 비밀, 사용자가 입력한 비밀)
"""

import json
import re
import secrets as _secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .models import IMAGE_TAG_PATTERN
from .spec import check_value, parse_spec, reject

POSTGRES_IMAGE = "postgres:16-alpine"
DB_USER = "app"
DB_NAME = "app"
_DB_PASSWORD = re.compile(r"^[A-Za-z0-9]{16,128}$")
# 외부 DB 주소: 앱 전용 계정(소문자·숫자·밑줄), 영숫자 비밀번호, RDS 주소, SSL 필수. 환경 파일에 그대로 쓰이므로 엄격하게 검사한다.
_DATABASE_URL = re.compile(
    r"^postgresql://[a-z][a-z0-9_]{0,62}:[A-Za-z0-9]{16,128}@[a-z0-9][a-z0-9.-]{0,252}:[0-9]{4,5}/[a-z][a-z0-9_]{0,62}\?sslmode=require$"
)
# 소문자 DNS 이름(점으로 구분된 2개 이상의 라벨, 라벨은 63자 이하). Host(`...`) 규칙에 들어간다.
_HOST = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


@dataclass(frozen=True)
class RenderedStack:
    compose_yaml: str
    compose_env: str  # .env 내용. 비밀이 들어 있으니 서버에서 권한 600으로 저장
    app_env: str  # app.env 내용. 비밀이 들어 있으니 서버에서 권한 600으로 저장
    warnings: tuple[str, ...]
    generated: tuple[str, ...]  # 이번에 새로 생성한 비밀의 이름(값은 담지 않는다)


def _new_token() -> str:
    return _secrets.token_hex(24)  # 영문자와 숫자만이라 URL에 넣어도 안전하다


def _quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _env_line(name: str, value: str) -> str:
    return f"{name}='{value}'"


def render_stack(
    spec: Mapping[str, Any],
    *,
    host: str,
    image_tag: str,
    secrets: Mapping[str, str] | None = None,
    previous_env: Mapping[str, str] | None = None,
    database_url: str | None = None,
    new_token: Callable[[], str] = _new_token,
) -> RenderedStack:
    """명세를 파일로 바꾼다. 잘못된 입력은 SpecError(AdapterError 포함)를 던진다.

    host          앱의 공개 주소(예: todo.demo.onprem.anyship.cloud). 어댑터가 정한다.
    secrets       사용자가 이번 배포에 입력한 비밀(이름 -> 값)
    previous_env  서버에 이미 있는 .env와 app.env의 값. 생성한 비밀(SECRET_KEY,
                  POSTGRES_PASSWORD)을 매번 바꾸면 로그인 세션이 끊기고 DB에 접속할
                  수 없게 되므로, 있으면 그대로 재사용한다.
    database_url  외부 DB 주소(EC2 세트의 공유 RDS). 주면 명세에 postgres가 있을 때 db 컨테이너 대신
                  이 주소를 앱의 DATABASE_URL로 쓴다. 비밀번호가 들어 있어 app.env에만 쓴다.
    """
    if len(host) > 253 or not _HOST.match(host):
        raise reject(
            "invalid_host",
            "앱의 공개 주소 형식이 올바르지 않습니다.",
            hint="소문자, 숫자, 하이픈으로 된 도메인 이름이어야 합니다.",
        )
    if not re.match(IMAGE_TAG_PATTERN, image_tag):
        raise reject(
            "invalid_image_tag",
            "이미지 태그 형식이 올바르지 않습니다.",
            hint="커밋 SHA(16진수 7~40자)를 사용해 주세요.",
        )

    parsed = parse_spec(spec)
    provided = dict(secrets or {})
    previous = dict(previous_env or {})
    warnings = list(parsed.warnings)
    generated: list[str] = []

    app_lines: list[str] = []
    missing: list[str] = []
    expected_inputs: set[str] = set()
    for var in parsed.env:
        if not var.secret:
            value = var.value or ""
        elif var.generate:
            value = previous.get(var.name)
            if not value:
                value = new_token()
                generated.append(var.name)
        else:
            expected_inputs.add(var.name)
            value = provided.get(var.name)
            if value is None:
                missing.append(var.name)
                continue
        check_value(var.name, value)
        app_lines.append(_env_line(var.name, value))
    if missing:
        raise reject(
            "missing_secret",
            "필요한 비밀 값이 입력되지 않았습니다: " + ", ".join(missing),
            hint="배포 화면에서 해당 값을 입력해 주세요.",
        )
    unused = sorted(set(provided) - expected_inputs)
    if unused:
        warnings.append("명세에 없는 비밀 입력은 사용하지 않았습니다: " + ", ".join(unused))

    external_db = parsed.postgres and database_url is not None
    if external_db:
        if not _DATABASE_URL.match(database_url):
            raise reject("invalid_env_value", "DB 주소 형식이 올바르지 않습니다.")
        app_lines.append(_env_line("DATABASE_URL", database_url))
    elif database_url is not None:
        warnings.append("명세에 postgres가 없어 DB 주소를 사용하지 않았습니다.")

    compose_env = ""
    if parsed.postgres and not external_db:
        password = previous.get("POSTGRES_PASSWORD")
        if password:
            if not _DB_PASSWORD.match(password):
                raise reject(
                    "invalid_env_value",
                    "서버에 저장된 DB 비밀번호 형식이 올바르지 않습니다.",
                    hint="서버의 앱 디렉터리 .env 파일을 확인해 주세요.",
                )
        else:
            password = new_token()
            generated.append("POSTGRES_PASSWORD")
        compose_env = _env_line("POSTGRES_PASSWORD", password) + "\n"

    return RenderedStack(
        compose_yaml=_compose_yaml(
            parsed.app,
            image_tag,
            parsed.port,
            host,
            postgres=parsed.postgres and not external_db,
            internal_network=not external_db,
        ),
        compose_env=compose_env,
        app_env="".join(line + "\n" for line in app_lines),
        warnings=tuple(warnings),
        generated=tuple(generated),
    )


def _compose_yaml(
    app: str, image_tag: str, port: int, host: str, *, postgres: bool, internal_network: bool = True
) -> str:
    # 여기에 들어가는 값은 모두 검증된 형식(앱 이름, 태그, 포트, 호스트)이다.
    labels = [
        "traefik.enable=true",
        "traefik.docker.network=traefik",
        f"traefik.http.routers.{app}.rule=Host(`{host}`)",
        f"traefik.http.routers.{app}.entrypoints=websecure",
        f"traefik.http.routers.{app}.tls.certresolver=le",
        f"traefik.http.services.{app}.loadbalancer.server.port={port}",
    ]
    lines = [
        f"name: {app}",
        "services:",
        "  web:",
        f"    image: {_quote(f'{app}:{image_tag}')}",
        "    restart: unless-stopped",
        "    env_file:",
        "      - app.env",
        "    environment:",
        f"      PORT: {_quote(str(port))}",
    ]
    if postgres:
        url = f"postgresql://{DB_USER}:${{POSTGRES_PASSWORD}}@db:5432/{DB_NAME}"
        lines += [
            f"      DATABASE_URL: {_quote(url)}",
            "    depends_on:",
            "      db:",
            "        condition: service_healthy",
        ]
    lines += [
        "    networks:",
        "      - traefik",
        *(["      - internal"] if internal_network else []),
        "    labels:",
        *[f"      - {_quote(label)}" for label in labels],
        "    read_only: true",
        "    tmpfs:",
        "      - /tmp",
        "    cap_drop:",
        "      - ALL",
        "    security_opt:",
        "      - no-new-privileges:true",
        "    mem_limit: 512m",
        "    cpus: 1.0",
        "    pids_limit: 200",
    ]
    if postgres:
        lines += [
            "  db:",
            f"    image: {_quote(POSTGRES_IMAGE)}",
            "    restart: unless-stopped",
            "    environment:",
            f"      POSTGRES_USER: {_quote(DB_USER)}",
            f"      POSTGRES_DB: {_quote(DB_NAME)}",
            '      POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"',
            "    volumes:",
            "      - db-data:/var/lib/postgresql/data",
            "    networks:",
            "      - internal",
            "    healthcheck:",
            f'      test: ["CMD-SHELL", "pg_isready -U {DB_USER} -d {DB_NAME}"]',
            "      interval: 5s",
            "      timeout: 3s",
            "      retries: 20",
            "    security_opt:",
            "      - no-new-privileges:true",
            "    mem_limit: 512m",
            "    pids_limit: 200",
        ]
    lines += ["networks:", "  traefik:", "    external: true"]
    if (
        internal_network
    ):  # 외부 DB를 쓰면 앱이 VPC의 RDS에 닿아야 하므로 막힌 네트워크를 만들지 않는다
        lines += [
            "  internal:",
            "    internal: true",
        ]  # 인터넷으로 나가는 길이 없는 앱 전용 네트워크
    if postgres:
        lines += ["volumes:", "  db-data: {}"]
    return "\n".join(lines) + "\n"

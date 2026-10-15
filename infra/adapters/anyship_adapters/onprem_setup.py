"""사용자가 자기 서버에서 한 번 실행할 준비 스크립트를 만든다(온프레미스 서버 등록 화면에 보여 줄 내용).

한 파일에 두 가지를 합친다. AWS 호스트가 첫 부팅에 하는 일(`infra/user-account/compute.tf`의 user_data)과 같은 구성이다.
  1. `infra/onprem-vm/scripts/setup.sh`: Docker, Compose, 비root `deploy` 계정(우리 **공개 키**만 허용), 키 로그인 전용 SSH
  2. `infra/sets/onprem/compose/traefik/compose.yaml`: 앱 앞에 서는 Traefik(HTTPS, Let's Encrypt)을 `/opt/apps/traefik`에 시작

스크립트에는 비밀이 없다(공개 키와 이메일 주소뿐). 끼워 넣는 값은 모두 형식을 검사해서 따옴표나 줄바꿈으로 스크립트를 깨뜨릴 수 없다.
원본 두 파일은 이 저장소의 `infra/`에 있고, 서비스가 저장소 그대로(편집 가능 설치) 돌 때 찾을 수 있다. 다른 곳에 두었다면 경로를 넘긴다.
"""

import base64
import re
from pathlib import Path

_INFRA = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = _INFRA / "onprem-vm" / "scripts" / "setup.sh"
TRAEFIK_COMPOSE = _INFRA / "sets" / "onprem" / "compose" / "traefik" / "compose.yaml"

# 공개 키 한 줄. 개인 키(`-----BEGIN ... PRIVATE KEY-----`)는 형식이 달라 통과하지 못한다.
_PUBLIC_KEY = re.compile(r"^ssh-ed25519 AAAA[A-Za-z0-9+/]{40,}={0,2}( [A-Za-z0-9._@:+-]{1,64})?$")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")


class SetupScriptError(ValueError):
    """끼워 넣을 값이 올바르지 않을 때. 문구에는 값을 싣지 않는다."""


def render_setup_script(
    public_key: str,
    acme_email: str,
    *,
    setup_script: Path = SETUP_SCRIPT,
    traefik_compose: Path = TRAEFIK_COMPOSE,
) -> str:
    if not isinstance(public_key, str) or not _PUBLIC_KEY.fullmatch(public_key.strip()):
        raise SetupScriptError(
            "공개 키는 `ssh-ed25519 AAAA...` 한 줄이어야 합니다(개인 키는 넣을 수 없습니다)."
        )
    if not isinstance(acme_email, str) or not _EMAIL.fullmatch(acme_email.strip()):
        raise SetupScriptError("인증서 안내를 받을 이메일 주소 형식이 올바르지 않습니다.")
    key, email = public_key.strip(), acme_email.strip()
    try:
        setup = Path(setup_script).read_text(encoding="utf-8")
        compose = Path(traefik_compose).read_bytes()
    except OSError:
        raise SetupScriptError("서버 준비 스크립트의 원본 파일을 읽을 수 없습니다.") from None
    body = (
        setup.split("\n", 1)[1] if setup.startswith("#!") else setup
    )  # 첫 줄의 셔뱅은 우리 것으로 바꾼다
    return "\n".join(
        [
            "#!/bin/bash",
            "# AnyShip 온프레미스 서버 준비 스크립트(자동 생성). 서버에서 root로 한 번 실행한다: sudo bash <이 파일>",
            "# 비밀이 없다: 공개 키와 이메일 주소만 들어 있다. 여러 번 실행해도 안전하다.",
            f"export DEPLOY_PUBLIC_KEY='{key}'",
            body.rstrip("\n"),
            "",
            "# --- Traefik: 앱 앞에서 HTTPS를 처리하는 공용 프록시(인증서는 처음에는 staging CA) ---",
            "install -d -m 755 -o deploy -g deploy /opt/apps/traefik",
            f"echo '{base64.b64encode(compose).decode()}' | base64 -d > /opt/apps/traefik/compose.yaml",
            f"printf 'ACME_EMAIL=%s\\n' '{email}' > /opt/apps/traefik/.env",
            "chown deploy:deploy /opt/apps/traefik/compose.yaml /opt/apps/traefik/.env",
            "runuser -u deploy -- bash -c 'cd /opt/apps/traefik && docker compose up -d'",
            'echo "--- traefik started"',
            "",
        ]
    )

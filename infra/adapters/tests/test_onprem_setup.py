import base64
import re
import shutil
import subprocess

import pytest

from anyship_adapters.onprem_setup import (
    SETUP_SCRIPT,
    TRAEFIK_COMPOSE,
    SetupScriptError,
    render_setup_script,
)

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dkJ3Bl1Z6 service-server-deploy"
EMAIL = "ops@example.com"


def test_the_script_holds_the_public_key_the_email_and_the_original_setup_steps():
    script = render_setup_script(KEY, EMAIL)
    assert script.startswith("#!/bin/bash\n") and f"export DEPLOY_PUBLIC_KEY='{KEY}'" in script
    assert f"printf 'ACME_EMAIL=%s\\n' '{EMAIL}' > /opt/apps/traefik/.env" in script
    original = SETUP_SCRIPT.read_text(encoding="utf-8").split("\n", 1)[1].rstrip("\n")
    assert original in script  # setup.sh를 고쳐 쓰지 않고 그대로 담는다
    assert script.count("#!") == 1  # 셔뱅은 맨 위 하나뿐이다(원본의 셔뱅은 걷어낸다)


def test_the_embedded_traefik_compose_file_is_exactly_the_repositorys_file():
    script = render_setup_script(KEY, EMAIL)
    encoded = re.search(
        r"echo '([A-Za-z0-9+/=]+)' \| base64 -d > /opt/apps/traefik/compose.yaml", script
    ).group(1)
    assert base64.b64decode(encoded) == TRAEFIK_COMPOSE.read_bytes()


def test_the_steps_come_in_the_order_prepare_the_server_then_start_traefik_as_the_deploy_account():
    script = render_setup_script(KEY, EMAIL)
    positions = [
        script.index(text)
        for text in (
            "DEPLOY_PUBLIC_KEY=",
            "dnf install -y docker",
            "useradd",
            "--- done",
            "base64 -d",
            "ACME_EMAIL",
            "runuser -u deploy",
            "--- traefik started",
        )
    ]
    assert positions == sorted(positions)


def test_the_same_inputs_give_the_same_script():
    assert render_setup_script(KEY, EMAIL) == render_setup_script(f"  {KEY}\n", f" {EMAIL} ")


def test_the_script_contains_no_secret_looking_content():
    script = render_setup_script(KEY, EMAIL)
    assert "PRIVATE KEY" not in script and not re.search(r"password\s*=", script, re.I)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash가 필요하다")
def test_the_script_is_valid_shell():
    # 표준 입력으로 넘긴다(Windows 경로의 백슬래시가 bash에서 깨지는 것을 피하고 줄바꿈도 그대로 보낸다).
    checked = subprocess.run(
        ["bash", "-n"], input=render_setup_script(KEY, EMAIL).encode(), capture_output=True
    )
    assert checked.returncode == 0, checked.stderr.decode(errors="replace")[:300]


@pytest.mark.parametrize(
    "key",
    [
        "",
        "ssh-rsa AAAAB3NzaC1yc2E" + "A" * 40,
        "ssh-ed25519 notbase64",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        KEY
        + "\nssh-ed25519 AAAA"
        + "A" * 40,  # 줄이 둘이면 authorized_keys에 다른 키를 끼워 넣을 수 있다
        KEY.replace("AAAA", "AAAA'; rm -rf /; echo '", 1),
        KEY + "'",
        KEY + " " + "x" * 80,
        None,
        12345,
    ],
)
def test_anything_but_a_single_ed25519_public_key_line_is_refused(key):
    with pytest.raises(SetupScriptError) as caught:
        render_setup_script(key, EMAIL)
    assert "BEGIN" not in str(caught.value) and "rm -rf" not in str(caught.value)


@pytest.mark.parametrize(
    "email",
    [
        "",
        "no-at-sign",
        "a b@example.com",
        "a@b",
        "a@example.com'; echo x; '",
        "a@example.com\nb",
        "@example.com",
        "x" * 70 + "@example.com",
        None,
        5,
    ],
)
def test_an_email_that_could_break_the_script_or_is_not_an_address_is_refused(email):
    with pytest.raises(SetupScriptError):
        render_setup_script(KEY, email)


def test_missing_source_files_are_reported_without_paths(tmp_path):
    with pytest.raises(SetupScriptError) as caught:
        render_setup_script(KEY, EMAIL, setup_script=tmp_path / "nope.sh")
    assert str(tmp_path) not in str(caught.value)
    with pytest.raises(SetupScriptError):
        render_setup_script(KEY, EMAIL, traefik_compose=tmp_path / "nope.yaml")


def test_a_setup_script_without_a_shebang_is_still_embedded_whole(tmp_path):
    plain = tmp_path / "setup.sh"
    plain.write_text("echo first\necho second\n", encoding="utf-8")
    script = render_setup_script(KEY, EMAIL, setup_script=plain)
    assert "echo first\necho second" in script and script.startswith("#!/bin/bash\n")


def test_the_script_fixes_its_own_umask_before_it_creates_anything():
    # 한 줄 명령이 umask 077을 걸고 sudo로 이 스크립트를 부르면, 폴더가 root 전용(700)이 되어 deploy 계정이
    # Compose 플러그인을 찾지 못한다(실서버에서 재현). 그래서 만들기 전에 umask를 고정해야 한다.
    script = render_setup_script(KEY, EMAIL)
    first_creation = min(script.index(text) for text in ("mkdir -p", "install -d", "install -m"))
    assert script.index("\numask 022\n") < first_creation

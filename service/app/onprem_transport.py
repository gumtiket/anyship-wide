"""온프레미스 전송 방식의 세부 사항을 다루는 유일한 서비스 모듈."""

import ipaddress
import json
import shlex
from pathlib import Path

from anyship_adapters import OnpremEnvironment as AdapterEnvironment
from anyship_adapters.compose_host import ComposeHost
from anyship_adapters.onprem_setup import render_setup_script
from anyship_adapters.ssh import SshConnection, SshRunner

from .deploy_state import DeployStateError


def require_kind(kind):
    if kind != "ssh":
        raise DeployStateError("connection_kind_unsupported", "지원하지 않는 연결 방식입니다.")


def public_address(value):
    try:
        address = ipaddress.IPv4Address(value)
        if not address.is_global:
            raise ValueError()
        return str(address)
    except (ValueError, TypeError):
        raise DeployStateError("invalid_public_ip", "공인 IPv4 주소가 필요합니다.") from None


def record_report(row, address):
    require_kind(row.connection_kind)
    row.host, row.ssh_user, row.ssh_port = public_address(address), "deploy", 22


def verified_address(row, result):
    require_kind(row.connection_kind)
    address = public_address(result.get("details", {}).get("public_ip"))
    if address != row.host:
        raise DeployStateError("public_ip_mismatch", "서버의 공인 IP를 확인하지 못했습니다.")
    return address


def environment(row, *, require_verified=True, cleanup=False):
    require_kind(row.connection_kind)
    if row.deleted_at is not None or (require_verified and row.status != "VERIFIED"):
        raise DeployStateError(
            "environment_not_connected", "연결이 확인된 환경만 사용할 수 있습니다."
        )
    if not cleanup and row.status == "ISSUED":
        raise DeployStateError("environment_not_signaled", "서버 준비 신고를 먼저 완료하세요.")
    if cleanup and row.host is None:
        # DNS 정리는 env_id만 쓰고 이 자리표시 주소에는 접속하지 않는다.
        return AdapterEnvironment(env_id=row.env_id, host="unregistered.invalid")
    try:
        return AdapterEnvironment(
            env_id=row.env_id,
            host=public_address(row.host),
            ssh_user=row.ssh_user or "deploy",
            ssh_port=row.ssh_port or 22,
        )
    except ValueError:
        raise DeployStateError(
            "environment_invalid", "저장된 환경의 연결 정보를 확인해 주세요."
        ) from None


def guidance(kind):
    require_kind(kind)
    return {
        "title": "서버 준비",
        "message": "Amazon Linux 2023(x86_64) 서버에서 실행하세요. Docker·deploy 계정·Traefik을 설치하고 서버 전체의 비밀번호 및 root SSH 로그인을 끕니다. SSH 22번과 앱 80/443번 포트, 공인 IPv4가 필요합니다. 기존 관리 접속을 유지한 상태에서 실행하세요. 모든 환경은 서비스의 공유 SSH 키를 사용합니다. 인증서는 기본 staging 설정이므로 운영 인증서 전환이 필요합니다.",
    }


def error_message(kind, code, fallback="연결 작업을 완료하지 못했습니다."):
    require_kind(kind)
    return {
        "ssh_unreachable": "서버 주소와 SSH 22번 포트·방화벽을 확인하세요.",
        "ssh_command_failed": "deploy 계정 권한과 서버 준비 상태를 확인하세요.",
    }.get(code, fallback)


def present_result(kind, data):
    if data.get("error"):
        error = data["error"]
        message = error_message(kind, error["code"], error.get("message", "작업에 실패했습니다."))
        if error["code"] in ("ssh_unreachable", "ssh_command_failed"):
            error = {**error, "message": message, "hint": message}
        data = {**data, "error": error}
    return data


class Transport:
    def __init__(self, settings):
        self.settings = settings

    def connect(self, env):
        runner = SshRunner(
            SshConnection(
                public_address(env.host),
                self.settings.deploy_ssh_key,
                user=env.ssh_user,
                port=env.ssh_port,
            )
        )
        return runner, ComposeHost(runner)

    def script(self, row, token):
        require_kind(row.connection_kind)
        key = Path(str(self.settings.deploy_ssh_key) + ".pub").read_text(encoding="utf-8").strip()
        script = render_setup_script(key, row.email)
        url = shlex.quote(self.settings.app_origin + f"/api/onprem/environments/{row.id}/ready")
        # URL과 접근 로그에 토큰을 남기지 않는다. 원본 토큰은 이 응답과 대상 서버에만 있다.
        script = script.replace(
            "# 비밀이 없다: 공개 키와 이메일 주소만 들어 있다. 여러 번 실행해도 안전하다.",
            "# 일회용 등록 토큰이 포함됩니다. 파일을 공유하거나 로그에 남기지 마세요.",
        )
        return (
            script
            + "\nset +x\n"
            + "public_ip=$(curl -4fsS --max-time 15 https://checkip.amazonaws.com)\n"
            + (
                'printf \'{"token":"%s","public_ip":"%s"}\' '
                + shlex.quote(token)
                + ' "$public_ip" | '
                f"curl -fsS --max-time 30 -H 'Content-Type: application/json' --data-binary @- {url}\n"
            )
        )

    def command(self, row, token):
        require_kind(row.connection_kind)
        url = shlex.quote(self.settings.app_origin + f"/api/onprem/environments/{row.id}/setup")
        payload = shlex.quote(json.dumps({"token": token}))
        # 다 내려받은 뒤에 실행한다. 임시 파일은 소유자만 읽을 수 있고 항상 지운다.
        body = (
            "set -e; umask 077; f=$(mktemp); trap 'rm -f \"$f\"' EXIT; "
            f"printf %s {payload} | curl -fsS --max-time 30 -H 'Content-Type: application/json' --data-binary @- {url} "
            '-o "$f"; sudo bash "$f"'
        )
        return "bash -c " + shlex.quote(body)

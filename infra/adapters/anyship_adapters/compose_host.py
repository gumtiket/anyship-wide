"""서버 한 대 위의 Compose 앱을 다루는 부품(온프레미스와 aws-always-on이 함께 쓴다).

SshRunner 위에서 하는 일: 앱 디렉터리와 파일 쓰기, 서버에 남은 이전 비밀 읽기, 이미지 전달,
compose 실행, 마이그레이션, 헬스체크. 어떤 환경인지(주소 규칙, DB 방식)는 모른다.
"""

import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from .compose import RenderedStack
from .models import APP_NAME_PATTERN
from .ssh import MAX_OUTPUT, CommandResult, SshRunner, safe_env

APPS_DIR = "/opt/apps"
_APP = re.compile(APP_NAME_PATTERN)
_IMAGE = re.compile(r"^[a-z][a-z0-9-]{2,62}:[0-9a-f]{7,40}$")  # <앱>:<커밋 SHA>
_ENV_LINE = re.compile(r"^([A-Z_][A-Z0-9_]{0,63})='([^'\r\n\0]*)'$")
_TAG = re.compile(r"^[0-9a-f]{7,40}$")


class ComposeHost:
    def __init__(self, ssh: SshRunner, *, apps_dir: str = APPS_DIR, popen=subprocess.Popen):
        self._ssh = ssh
        self._dir = apps_dir
        self._popen = popen  # 시험에서는 가짜로 바꿔 끼운다

    def _app_dir(self, app: str) -> str:
        if not _APP.match(app):
            raise ValueError("invalid app name")
        return f"{self._dir}/{app}"

    def read_previous_env(self, app: str) -> dict[str, str]:
        """서버에 이미 있는 .env와 app.env의 값. 생성한 비밀을 재배포 때 재사용하려고 읽는다.
        비밀이 들어 있으니 메모리에서만 쓰고 로그에 남기지 않는다."""
        values: dict[str, str] = {}
        for name in (".env", "app.env"):
            done = self._ssh.run(["cat", f"{self._app_dir(app)}/{name}"])
            for line in done.stdout.splitlines() if done.ok else []:
                match = _ENV_LINE.match(line)
                if match:
                    values[match.group(1)] = match.group(2)
        return values

    def write_stack(self, app: str, stack: RenderedStack) -> CommandResult:
        """앱 디렉터리를 만들고 파일을 쓴다. 비밀이 든 파일(.env, app.env)은 권한 600."""
        directory = self._app_dir(app)
        result = self._ssh.run(["mkdir", "-p", directory])
        for name, content, mode in (
            (".env", stack.compose_env, "0600"),
            ("app.env", stack.app_env, "0600"),
            ("compose.yaml", stack.compose_yaml, "0644"),
        ):
            if not result.ok:
                break
            result = self._ssh.put(f"{directory}/{name}", content, mode=mode)
        return result

    def load_image(self, image: str, *, timeout: float = 900) -> CommandResult:
        """서비스 서버의 이미지를 서버로 흘려보낸다(docker save | ssh docker load, 레지스트리 없음)."""
        if not _IMAGE.match(image):
            raise ValueError("invalid image reference")
        save = self._popen(
            ["docker", "save", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=safe_env(),
        )
        try:
            loaded = self._ssh.run(["docker", "load"], stdin=save.stdout, timeout=timeout)
        finally:
            save.stdout.close()
        if loaded.timed_out:  # 로컬 docker save도 멈추고, 결과는 실패가 아니라 시간 초과로 알린다
            save.kill()
            save.wait()
            return loaded
        errors = save.stderr.read().decode("utf-8", errors="replace")[:MAX_OUTPUT]
        code = save.wait()
        return loaded if code == 0 else CommandResult(code, stderr=errors)

    def up(self, app: str, *, timeout: float = 300) -> CommandResult:
        return self._ssh.run(
            [
                "docker",
                "compose",
                "--project-directory",
                self._app_dir(app),
                "up",
                "-d",
                "--remove-orphans",
            ],
            timeout=timeout,
        )

    def migrate(self, app: str, command: str, *, timeout: float = 300) -> CommandResult:
        """앱 컨테이너 안에서만 마이그레이션 명령을 실행한다(서버의 셸에서 실행하지 않는다).

        `run`이 만드는 임시 컨테이너는 compose.yaml의 Traefik 라벨을 그대로 물려받는다. 그러면 Traefik이 같은 서비스의
        두 번째 서버로 등록했다가, 컨테이너가 사라진 뒤 설정이 갱신되기 전까지 없는 주소로 요청을 보내 502가 한 번씩 난다
        (실제 호스트에서 재현했다). 그래서 임시 컨테이너에서만 Traefik을 끈다."""
        return self._ssh.run(
            [
                "docker",
                "compose",
                "--project-directory",
                self._app_dir(app),
                "run",
                "--rm",
                "--label",
                "traefik.enable=false",
                "web",
                "sh",
                "-c",
                command,
            ],
            timeout=timeout,
        )

    # -- 이미 배포된 앱을 살펴보고 바꾸고 지우는 도구 ------------------------------------------
    def exists(self, app: str) -> bool:
        return self._ssh.run(["test", "-f", f"{self._app_dir(app)}/compose.yaml"]).ok

    def _image_line(self, app: str) -> re.Pattern:
        # render_stack이 쓴 웹 앱의 이미지 줄: `    image: "<앱>:<커밋 SHA>"`
        return re.compile(rf'^    image: "{re.escape(app)}:([0-9a-f]{{7,40}})"$', re.MULTILINE)

    def current_image_tag(self, app: str) -> str | None:
        done = self._ssh.run(["cat", f"{self._app_dir(app)}/compose.yaml"])
        found = self._image_line(app).search(done.stdout) if done.ok else None
        return found.group(1) if found else None

    def set_image_tag(self, app: str, image_tag: str) -> CommandResult:
        """서버의 compose.yaml에서 앱 이미지 태그만 바꾼다(비밀과 DB 설정은 그대로 둔다)."""
        if not _TAG.match(image_tag):
            raise ValueError("invalid image tag")
        path = f"{self._app_dir(app)}/compose.yaml"
        done = self._ssh.run(["cat", path])
        if not done.ok:
            return done
        updated, count = self._image_line(app).subn(f'    image: "{app}:{image_tag}"', done.stdout)
        if count != 1:
            return CommandResult(1, stderr="compose.yaml에서 앱 이미지 줄을 찾지 못했습니다")
        return self._ssh.put(path, updated, mode="0644")

    def running_services(self, app: str) -> set[str]:
        done = self._ssh.run(
            [
                "docker",
                "compose",
                "--project-directory",
                self._app_dir(app),
                "ps",
                "--status",
                "running",
                "--services",
            ]
        )
        return set(done.stdout.split()) if done.ok else set()

    def image_present(self, image: str) -> bool:
        if not _IMAGE.match(image):
            raise ValueError("invalid image reference")
        return self._ssh.run(["docker", "image", "inspect", "--format", "{{.Id}}", image]).ok

    def stop_service(self, app: str, service: str = "web", *, timeout: float = 60) -> CommandResult:
        """앱 컨테이너 하나만 멈춘다(컨테이너와 볼륨은 지우지 않는다). 데이터를 옮기는 동안 쓰기를 막으려는 용도다."""
        if service not in ("web", "db"):
            raise ValueError("invalid service")
        return self._ssh.run(
            ["docker", "compose", "--project-directory", self._app_dir(app), "stop", service],
            timeout=timeout,
        )

    def down(self, app: str, *, timeout: float = 120) -> CommandResult:
        """컨테이너, 네트워크, DB 볼륨(데이터)을 모두 지운다."""
        return self._ssh.run(
            [
                "docker",
                "compose",
                "--project-directory",
                self._app_dir(app),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            timeout=timeout,
        )

    def remove_dir(self, app: str) -> CommandResult:
        return self._ssh.run(["rm", "-r", "-f", self._app_dir(app)])


def _status(url: str, verify_tls: bool, timeout: float = 5) -> int | None:
    context = None if verify_tls else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return None


def wait_healthy(
    url: str,
    *,
    attempts: int = 30,
    delay: float = 2.0,
    verify_tls: bool = True,
    fetch: Callable[[str, bool], int | None] = _status,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, int | None]:
    """서비스 서버에서 공개 주소로 요청해 200이 올 때까지 기다린다. (성공 여부, 마지막 상태 코드)

    verify_tls=False는 Let's Encrypt staging 인증서를 시험할 때만 쓴다."""
    if not url.startswith("https://"):
        raise ValueError("health check url must be https")
    status = None
    for attempt in range(attempts):
        status = fetch(url, verify_tls)
        if status == 200:
            return True, status
        if attempt < attempts - 1:
            sleep(delay)
    return False, status

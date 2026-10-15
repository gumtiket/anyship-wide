"""여러 테스트가 같이 쓰는 가짜 서버와 가짜 프로세스. 실제 SSH나 Docker는 쓰지 않는다."""

import io
import shlex
import subprocess
from pathlib import Path


class FakeServer:
    """가짜 SSH 실행기: 서버의 파일을 딕셔너리로 흉내 내고, 받은 명령을 기록한다.

    responses  {명령의 앞부분(튜플): (종료 코드, stdout, stderr)}. 앞부분이 맞는 명령은 이 값으로 답한다.
    """

    def __init__(self, fail_path_suffix=None, responses=None):
        self.files: dict[str, bytes] = {}
        self.commands: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.responses: dict[tuple, tuple] = dict(responses or {})
        self._fail_suffix = fail_path_suffix

    def __call__(self, cmd, **kwargs):
        remote = shlex.split(cmd[-1])
        self.commands.append(remote)
        self.kwargs.append(kwargs)
        for prefix, (code, out, err) in self.responses.items():
            if tuple(remote[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=err)
        if remote[0] == "cat":
            found = remote[1] in self.files
            return subprocess.CompletedProcess(
                cmd, 0 if found else 1, stdout=self.files.get(remote[1], b""), stderr=b""
            )
        if remote[:2] == ["sh", "-c"]:  # SshRunner.put이 만든 파일 쓰기 명령
            path = remote[4]
            if self._fail_suffix and path.endswith(self._fail_suffix):
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"disk full")
            self.files[path] = kwargs["input"]
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    def written(self):
        return [Path(p).name for p in self.files]

    def last(self):
        return self.commands[-1]


class FakePopen:
    """로컬 `docker save`를 흉내 낸다."""

    def __init__(self, cmd, *, code=0, image=b"IMAGE-BYTES", errors=b"", **kwargs):
        self.cmd, self.kwargs, self.code, self.killed = cmd, kwargs, code, False
        self.stdout, self.stderr = io.BytesIO(image), io.BytesIO(errors)

    def kill(self):
        self.killed = True

    def wait(self):
        return self.code


class Log:
    """진행 로그를 모아 두는 로그 함수."""

    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    def text(self):
        return "".join(e.model_dump_json() for e in self.events)

    def steps(self):
        return [(e.step, e.total, e.name) for e in self.events if e.step]


class LifecycleServer(FakeServer):
    """파일뿐 아니라 이미지 보유, 실행 중인 서비스, 삭제까지 기억하는 가짜 서버.

    배포 -> 상태 -> 롤백 -> 삭제의 흐름을 시험하려고 쓴다. responses로 지정한 명령은 항상 그 답이 우선한다.
    """

    def __init__(self, responses=None, **kwargs):
        defaults = {
            ("docker", "ps"): (0, b"traefik-traefik-1\n", b""),
            ("curl",): (0, b"3.38.88.141\n", b""),
        }
        super().__init__(responses={**defaults, **(responses or {})}, **kwargs)
        self.images: set[str] = set()
        self.running: set[str] = set()
        self.downed: list[str] = []
        self.removed: list[str] = []
        self._pending_image = None

    def popen(self, cmd, **kwargs):
        """ComposeHost의 popen으로 넘겨서, 어떤 이미지를 보냈는지 기억한다."""
        self._pending_image = cmd[-1]
        return FakePopen(cmd, **kwargs)

    def _answer(self, cmd, code=0, out=b""):
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=b"")

    def __call__(self, cmd, **kwargs):
        remote = shlex.split(cmd[-1])
        if any(tuple(remote[: len(prefix)]) == prefix for prefix in self.responses):
            return super().__call__(cmd, **kwargs)
        compose = remote[:2] == ["docker", "compose"]
        handled = True
        if remote[:3] == ["docker", "image", "inspect"]:
            answer = self._answer(cmd, 0 if remote[-1] in self.images else 1)
        elif remote == ["docker", "load"]:
            self.images.add(self._pending_image)
            answer = self._answer(cmd)
        elif remote[:2] == ["test", "-f"]:
            answer = self._answer(cmd, 0 if remote[2] in self.files else 1)
        elif compose and "ps" in remote:
            answer = self._answer(cmd, 0, ("\n".join(sorted(self.running)) + "\n").encode())
        elif compose and "up" in remote:
            self.running = {"web", "db"}
            answer = self._answer(cmd)
        elif compose and "down" in remote:
            self.running = set()
            self.downed.append(remote[remote.index("--project-directory") + 1])
            answer = self._answer(cmd)
        elif remote[:3] == ["rm", "-r", "-f"]:
            target = remote[3]
            self.files = {
                path: data for path, data in self.files.items() if not path.startswith(target + "/")
            }
            self.removed.append(target)
            answer = self._answer(cmd)
        else:
            handled = False
        if not handled:
            return super().__call__(cmd, **kwargs)
        self.commands.append(remote)
        self.kwargs.append(kwargs)
        return answer

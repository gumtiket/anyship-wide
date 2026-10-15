"""요청 격리: 새 출력 디렉터리, 원자적 게시, 소유한 임시 파일 정리."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai.build_context import BuildContext
    from ai.models import AnalysisResult

_clients_guard = Lock()
_active_clients: set[int] = set()
_contexts: ContextVar[list[BuildContext] | None] = ContextVar("analysis_contexts", default=None)


def register_context(context: BuildContext) -> None:
    owned = _contexts.get()
    if owned is not None:
        owned.append(context)


def isolated_output(function: Callable[..., AnalysisResult]) -> Callable[..., AnalysisResult]:
    @wraps(function)
    def wrapped(repo_path: str | Path, **kwargs) -> AnalysisResult:
        repo = Path(repo_path).resolve()
        output = Path(kwargs.get("out_dir", "out")).resolve()
        if not repo.is_dir():
            raise ValueError("입력 레포 경로는 존재하는 디렉터리여야 합니다.")
        if output.is_relative_to(repo) or repo.is_relative_to(output):
            raise ValueError("원본 보호: 출력은 입력 레포와 분리된 디렉터리에 지정하세요.")
        if output.is_dir() and any(p.is_symlink() for p in output.iterdir()):
            raise ValueError("원본 보호: 출력 파일 심볼릭 링크는 허용하지 않습니다.")
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError("output_not_empty: 요청마다 새 출력 디렉터리를 사용하세요.")
        client_ids = {
            id(kwargs[key])
            for key in ("llm", "artifact_llm", "decision_llm", "repair_llm")
            if kwargs.get(key) is not None
        }
        with _clients_guard:
            if client_ids & _active_clients:
                raise ValueError("llm_client_in_use: 요청마다 새 LLM 클라이언트를 사용하세요.")
            _active_clients.update(client_ids)
        lock_fd = None
        lock_path = output.parent / (
            ".bronze-output-" + hashlib.sha256(output.name.encode()).hexdigest()[:16] + ".lock"
        )
        owned = []
        token = _contexts.set(owned)
        result = None
        published = False
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                raise ValueError(
                    "output_in_use: 출력 디렉터리를 다른 요청이 사용 중입니다."
                ) from None
            if output.exists() and (not output.is_dir() or any(output.iterdir())):
                raise ValueError("output_not_empty: 요청마다 새 출력 디렉터리를 사용하세요.")
            with TemporaryDirectory(prefix=".bronze-analysis-", dir=output.parent) as temporary:
                result = function(repo_path, **{**kwargs, "out_dir": temporary})
                # 7종 결과와 선택적 trace·출처 파일을 한꺼번에 게시한다.
                result.output_files = {name: str(output / name) for name in result.output_files}
                if output.exists():
                    output.rmdir()  # 호출한 쪽이 고른 빈 디렉터리만 교체할 수 있다.
                os.rename(temporary, output)
                published = True
            return result
        finally:
            for context in owned:
                if not published or result is None or context is not result.build_context:
                    context.cleanup()
            _contexts.reset(token)
            if lock_fd is not None:
                os.close(lock_fd)
                lock_path.unlink(missing_ok=True)
            with _clients_guard:
                _active_clients.difference_update(client_ids)

    return wrapped

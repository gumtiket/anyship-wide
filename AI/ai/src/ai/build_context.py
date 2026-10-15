import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ConfigDict, PrivateAttr


class BuildContext(BaseModel):
    """제안을 반영한 비공개 샘플 트리의 소유자. 게이트에서 쓴 뒤 호출한 쪽이 정리해야 한다."""

    model_config = ConfigDict(extra="forbid")
    root: str
    dockerfile: str = "Dockerfile"
    _temporary: TemporaryDirectory | None = PrivateAttr(default=None)
    _sample_name: str | None = PrivateAttr(default=None)
    _sealed_digest: str | None = PrivateAttr(default=None)

    def tree_digest(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(Path(self.root).rglob("*")):
            if path.is_symlink():
                raise ValueError("gate_context_symlink")
            if path.is_file():
                digest.update(path.relative_to(self.root).as_posix().encode() + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    def verify_sample(self) -> bool:
        return (
            self._sample_name in {"todo", "todo-scheduler"}
            and self._sealed_digest == self.tree_digest()
        )

    def cleanup(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def __enter__(self) -> "BuildContext":
        return self

    def __exit__(self, *args) -> None:
        self.cleanup()

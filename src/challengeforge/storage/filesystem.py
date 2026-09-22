from __future__ import annotations

from pathlib import Path


class LocalFilesystemStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        if not key or key.startswith("/") or ".." in Path(key).parts:
            raise ValueError("Invalid artifact key.")
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError("Artifact key escapes storage root.")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        meta = path.with_suffix(path.suffix + ".content_type")
        meta.write_text(content_type or "application/octet-stream", encoding="utf-8")
        return key

    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

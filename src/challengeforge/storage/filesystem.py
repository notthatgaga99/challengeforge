from __future__ import annotations

from pathlib import Path
from typing import Iterable
import os
import shutil


class LocalFilesystemStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        if not key or key.startswith("/") or ".." in Path(key).parts:
            raise ValueError("Invalid artifact key.")
        root = self.root.resolve()
        path = (root / key).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("Artifact key escapes storage root.") from exc
        return path

    def _sidecar(self, path: Path) -> Path:
        return path.with_suffix(path.suffix + ".content_type")

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp sibling then replace for crash-safer PUT.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        meta = self._sidecar(path)
        meta.write_text(content_type or "application/octet-stream", encoding="utf-8")
        return key

    def put_from_path(self, key: str, source: Path, content_type: str) -> str:
        """Atomically publish a complete local file as ``key`` (no partial final)."""
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        src = Path(source)
        if not src.is_file():
            raise FileNotFoundError(str(source))
        tmp = path.with_suffix(path.suffix + ".tmp")
        if tmp.exists():
            tmp.unlink()
        try:
            os.replace(src, tmp)
        except OSError:
            shutil.copyfile(src, tmp)
            try:
                src.unlink()
            except OSError:
                pass
        os.replace(tmp, path)
        meta = self._sidecar(path)
        meta.write_text(content_type or "application/octet-stream", encoding="utf-8")
        return key

    def incoming_root(self) -> Path:
        root = self.root / ".incoming"
        root.mkdir(parents=True, exist_ok=True)
        return root


    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def delete(self, key: str) -> bool:
        path = self._resolve(key)
        removed = False
        sidecar = self._sidecar(path)
        if path.is_file():
            path.unlink()
            removed = True
        if sidecar.is_file():
            sidecar.unlink()
            removed = True
        # Drop empty parent dirs under root (best-effort).
        parent = path.parent
        root = self.root.resolve()
        while parent != root and parent.is_dir():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return removed

    def iter_keys(self, prefix: str = "") -> Iterable[str]:
        root = self.root.resolve()
        base = root
        if prefix:
            if prefix.startswith("/") or ".." in Path(prefix).parts:
                raise ValueError("Invalid prefix.")
            base = (root / prefix).resolve()
            try:
                base.relative_to(root)
            except ValueError as exc:
                raise ValueError("Prefix escapes storage root.") from exc
        if not base.exists():
            return
        try:
            paths = list(base.rglob("*"))
        except FileNotFoundError:
            return
        for path in paths:
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            name = path.name
            if name.endswith(".content_type") or name.endswith(".tmp"):
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            yield rel

    def mtime(self, key: str) -> float | None:
        path = self._resolve(key)
        if not path.is_file():
            return None
        return path.stat().st_mtime

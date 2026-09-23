"""Streaming upload staging: temp bytes → finalize → final artifact key.

Streaming ≠ resumability. This module streams request body to disk so peak RSS
does not track artifact size, then atomically publishes a complete blob under
the final key. Abandoned ``.incoming/`` objects are reclaimable by cleanup.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, BinaryIO
from uuid import uuid4

from challengeforge.domain.exceptions import ValidationFailed


INCOMING_PREFIX = ".incoming/"
DEFAULT_CHUNK = 64 * 1024


@dataclass
class IncomingUpload:
    """A fully written temporary object not yet published as an artifact key."""

    staging_path: Path
    size: int
    sha256: str
    content_type: str
    _finalized: bool = False

    @property
    def finalized(self) -> bool:
        return self._finalized

    def mark_finalized(self) -> None:
        self._finalized = True

    def abort(self) -> None:
        """Remove staging bytes if not finalized."""
        if self._finalized:
            return
        try:
            if self.staging_path.is_file():
                self.staging_path.unlink()
            meta = Path(str(self.staging_path) + ".meta")
            if meta.is_file():
                meta.unlink()
            parent = self.staging_path.parent
            if parent.is_dir():
                try:
                    parent.rmdir()
                except OSError:
                    pass
        except OSError:
            pass


def _sha256_file(path: Path, chunk_size: int = DEFAULT_CHUNK) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


async def stream_to_incoming(
    incoming_root: Path,
    chunks: AsyncIterator[bytes],
    *,
    max_bytes: int,
    content_type: str = "application/octet-stream",
    expected_sha256: str | None = None,
    chunk_size: int = DEFAULT_CHUNK,
) -> IncomingUpload:
    """Stream async chunks into ``incoming_root / {uuid}`` with size + hash checks.

    Raises ValidationFailed if over size or checksum mismatch.
    On any error, staging is deleted before re-raise.
    """
    incoming_root.mkdir(parents=True, exist_ok=True)
    upload_id = str(uuid4())
    staging = incoming_root / upload_id
    hasher = hashlib.sha256()
    size = 0
    try:
        with staging.open("wb") as fh:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise ValidationFailed("Artifact exceeds the size limit.")
                fh.write(chunk)
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if expected_sha256 and expected_sha256.lower() != digest:
            raise ValidationFailed("Artifact checksum mismatch.")
        return IncomingUpload(
            staging_path=staging,
            size=size,
            sha256=digest,
            content_type=content_type or "application/octet-stream",
        )
    except Exception:
        try:
            if staging.is_file():
                staging.unlink()
        except OSError:
            pass
        raise


async def iter_upload_file(upload_file, chunk_size: int = DEFAULT_CHUNK) -> AsyncIterator[bytes]:
    """Yield chunks from a Starlette/FastAPI UploadFile."""
    while True:
        chunk = await upload_file.read(chunk_size)
        if not chunk:
            break
        yield chunk


def sync_stream_to_incoming(
    incoming_root: Path,
    reader: BinaryIO,
    *,
    max_bytes: int,
    content_type: str = "application/octet-stream",
    expected_sha256: str | None = None,
    chunk_size: int = DEFAULT_CHUNK,
) -> IncomingUpload:
    """Sync variant for benchmarks/tests."""
    incoming_root.mkdir(parents=True, exist_ok=True)
    upload_id = str(uuid4())
    staging = incoming_root / upload_id
    hasher = hashlib.sha256()
    size = 0
    try:
        with staging.open("wb") as fh:
            while True:
                chunk = reader.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValidationFailed("Artifact exceeds the size limit.")
                fh.write(chunk)
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if expected_sha256 and expected_sha256.lower() != digest:
            raise ValidationFailed("Artifact checksum mismatch.")
        return IncomingUpload(
            staging_path=staging,
            size=size,
            sha256=digest,
            content_type=content_type or "application/octet-stream",
        )
    except Exception:
        try:
            if staging.is_file():
                staging.unlink()
        except OSError:
            pass
        raise


def buffered_peak_simulation(data: bytes) -> tuple[int, str]:
    """Baseline: hold full bytes in memory (current API pattern). Returns (len, sha256)."""
    return len(data), hashlib.sha256(data).hexdigest()


def cleanup_incoming(
    incoming_root: Path,
    *,
    grace_seconds: float = 3600.0,
    now: float | None = None,
) -> list[str]:
    """Delete abandoned staging files older than grace. Returns deleted names."""
    import time

    if not incoming_root.exists():
        return []
    clock = time.time() if now is None else now
    deleted: list[str] = []
    for path in list(incoming_root.glob("*")):
        if not path.is_file():
            continue
        if path.name.endswith(".meta"):
            continue
        try:
            age = clock - path.stat().st_mtime
        except OSError:
            continue
        if age < grace_seconds:
            continue
        try:
            path.unlink()
            deleted.append(path.name)
            meta = Path(str(path) + ".meta")
            if meta.is_file():
                meta.unlink()
        except OSError:
            pass
    return deleted

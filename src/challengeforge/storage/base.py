from typing import Iterable, Protocol
from pathlib import Path


class ArtifactStorage(Protocol):
    """Replaceable blob store. Domain code depends on this protocol, not a vendor SDK."""

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Persist complete bytes and return the storage key."""

    def put_from_path(self, key: str, source: Path, content_type: str) -> str:
        """Publish a complete local file as ``key`` (atomic finalize)."""

    def get(self, key: str) -> bytes:
        """Return stored bytes or raise FileNotFoundError."""

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool:
        """Remove blob (+ sidecar if any). True if something was removed."""

    def iter_keys(self, prefix: str = "") -> Iterable[str]:
        """Yield blob keys under prefix (excludes content-type sidecars)."""

    def mtime(self, key: str) -> float | None:
        """Modification time of the blob, or None if missing."""

    def incoming_root(self) -> Path:
        """Directory for ephemeral staging uploads (not product artifacts)."""

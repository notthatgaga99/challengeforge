from typing import Iterable, Protocol


class ArtifactStorage(Protocol):
    """Replaceable blob store. Domain code depends on this protocol, not a vendor SDK."""

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Persist bytes and return the storage key."""

    def get(self, key: str) -> bytes:
        """Return stored bytes or raise FileNotFoundError."""

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool:
        """Remove blob (+ sidecar if any). True if something was removed."""

    def iter_keys(self, prefix: str = "") -> Iterable[str]:
        """Yield blob keys under prefix (excludes content-type sidecars)."""

    def mtime(self, key: str) -> float | None:
        """Modification time of the blob, or None if missing."""

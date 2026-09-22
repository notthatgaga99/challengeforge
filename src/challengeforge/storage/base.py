from typing import Protocol


class ArtifactStorage(Protocol):
    """Replaceable blob store. Domain code depends on this protocol, not a vendor SDK."""

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Persist bytes and return the storage key."""

    def get(self, key: str) -> bytes:
        """Return stored bytes or raise FileNotFoundError."""

    def exists(self, key: str) -> bool: ...

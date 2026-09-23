"""Artifact consistency: blob-first, compensating delete, orphan reconciliation.

Invariant
---------
A product-visible artifact exists iff:

1. a durable submission row references ``artifact_key``, AND
2. the blob exists in object storage.

Unreferenced blobs are garbage (orphans). Referenced missing blobs are
integrity failures and must not be introduced by write ordering.

Write order remains **blob then metadata** so a crash never leaves a
committed key pointing at missing bytes. Orphans are closed by:

* best-effort compensating ``delete`` when the metadata transaction fails
* deleting the previous key after a successful replace
* periodic reconciliation of unreferenced keys past a grace window

There is no async ingestion pipeline today; evaluation must not treat a blob
as authoritative until the submission row is committed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol


class ArtifactStorageOps(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool: ...

    def iter_keys(self, prefix: str = "") -> Iterable[str]: ...

    def mtime(self, key: str) -> float | None: ...


@dataclass
class ReconcileReport:
    scanned: int = 0
    referenced: int = 0
    orphan_candidates: int = 0
    deleted: list[str] = field(default_factory=list)
    missing_referenced: list[str] = field(default_factory=list)
    skipped_young: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "referenced": self.referenced,
            "orphan_candidates": self.orphan_candidates,
            "deleted": list(self.deleted),
            "missing_referenced": list(self.missing_referenced),
            "skipped_young": list(self.skipped_young),
            "errors": list(self.errors),
        }


def compensate_delete(storage: ArtifactStorageOps, key: str | None) -> bool:
    """Best-effort delete after a failed metadata commit. Never raises."""
    if not key:
        return False
    try:
        return bool(storage.delete(key))
    except Exception:
        return False


def reconcile_orphans(
    storage: ArtifactStorageOps,
    referenced_keys: Iterable[str | None],
    *,
    prefix: str = "submissions/",
    grace_seconds: float = 60.0,
    now: float | None = None,
    delete: bool = True,
) -> ReconcileReport:
    """Delete (or report) blobs under ``prefix`` not referenced by metadata.

    Young orphans inside ``grace_seconds`` are skipped so an in-flight
    blob-then-commit upload is not raced by the scanner.
    """
    refs = {k for k in referenced_keys if k}
    report = ReconcileReport(referenced=len(refs))
    clock = time.time() if now is None else now

    for key in refs:
        try:
            if not storage.exists(key):
                report.missing_referenced.append(key)
        except Exception as exc:
            report.errors.append(f"exists({key}): {exc}")

    for key in list(storage.iter_keys(prefix)):
        report.scanned += 1
        if key in refs:
            continue
        report.orphan_candidates += 1
        mtime = storage.mtime(key)
        if mtime is not None and (clock - mtime) < grace_seconds:
            report.skipped_young.append(key)
            continue
        if not delete:
            continue
        try:
            if storage.delete(key):
                report.deleted.append(key)
        except Exception as exc:
            report.errors.append(f"delete({key}): {exc}")
    return report


def is_sidecar_name(name: str) -> bool:
    return name.endswith(".content_type")

"""Synthetic ingestion stages: PARSE → NORMALIZE → READY output.

Not chunking/embeddings/RAG. Produces a durable JSON result under the artifact
store so effects are idempotent under at-least-once re-delivery:

  ingestion/{job_id}/result.json

Poison / transient control bytes in the artifact body (trusted synthetic only):

- prefix ``cf_ingest_poison`` → deterministic Failure (no retry)
- prefix ``cf_ingest_transient`` → transient until attempt_count >= 2
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from challengeforge.storage.base import ArtifactStorage


POISON_PREFIX = b"cf_ingest_poison"
TRANSIENT_PREFIX = b"cf_ingest_transient"


class DeterministicIngestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TransientIngestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class IngestResult:
    result_key: str
    parsed_bytes: int
    normalized_chars: int


def result_key_for(job_id: UUID) -> str:
    return f"ingestion/{job_id}/result.json"


def process_artifact(
    storage: ArtifactStorage,
    *,
    job_id: UUID,
    artifact_key: str,
    attempt_count: int,
) -> IngestResult:
    """At-least-once safe: overwrites the same result_key for the job."""
    try:
        raw = storage.get(artifact_key)
    except FileNotFoundError as exc:
        raise DeterministicIngestError(
            "missing_artifact", f"Artifact blob missing: {artifact_key}"
        ) from exc

    # PARSE
    if raw.startswith(POISON_PREFIX):
        raise DeterministicIngestError(
            "malformed_artifact", "Synthetic poison artifact (deterministic)."
        )
    if raw.startswith(TRANSIENT_PREFIX) and attempt_count < 2:
        raise TransientIngestError(
            "transient_backend",
            "Synthetic transient failure (retryable until attempt>=2).",
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeterministicIngestError(
            "invalid_encoding", "Artifact is not valid UTF-8."
        ) from exc

    # NORMALIZE
    normalized = " ".join(text.split())
    payload = {
        "job_id": str(job_id),
        "artifact_key": artifact_key,
        "stage": "READY",
        "parsed_bytes": len(raw),
        "normalized": normalized[:4096],
        "normalized_chars": len(normalized),
        "attempt_count": attempt_count,
    }

    # Durable output — write via staging path then put_from_path for complete-only.
    key = result_key_for(job_id)
    incoming = storage.incoming_root() / f"ingest-{job_id}"
    incoming.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    storage.put_from_path(key, incoming, "application/json")
    return IngestResult(
        result_key=key,
        parsed_bytes=len(raw),
        normalized_chars=len(normalized),
    )


def cleanup_partial_result(storage: ArtifactStorage, job_id: UUID) -> None:
    """Remove incomplete result if present (best-effort)."""
    key = result_key_for(job_id)
    try:
        storage.delete(key)
    except Exception:
        pass
    # Also drop leftover incoming staging
    try:
        path = storage.incoming_root() / f"ingest-{job_id}"
        if path.is_file():
            path.unlink()
    except Exception:
        pass

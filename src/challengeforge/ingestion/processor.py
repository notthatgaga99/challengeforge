"""Ingestion stages: PARSE → NORMALIZE → CHUNK → READY.

Durable outputs per job:

- ``ingestion/{job_id}/canonical.txt`` — structure-preserving text
- ``ingestion/{job_id}/result.json`` — manifest (versions, counts, hashes)
- PostgreSQL ``document_chunks`` rows (written by worker in same txn as succeed)

Poison / transient control prefixes (trusted synthetic only) unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from challengeforge.ingestion.canonical import (
    PARSER_VERSION,
    normalize_to_canonical,
)
from challengeforge.ingestion.chunking import (
    CHUNKER_VERSION,
    DEFAULT_MAX_CHARS,
    ChunkDraft,
    chunk_hybrid,
)
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
    canonical_key: str
    parser_version: str
    chunker_version: str
    canonical_chars: int
    canonical_sha256: str
    content_kind: str
    chunk_count: int
    max_chunk_chars: int
    chunks: tuple[ChunkDraft, ...]


def result_key_for(job_id: UUID) -> str:
    return f"ingestion/{job_id}/result.json"


def canonical_key_for(job_id: UUID) -> str:
    return f"ingestion/{job_id}/canonical.txt"


def process_artifact(
    storage: ArtifactStorage,
    *,
    job_id: UUID,
    artifact_key: str,
    attempt_count: int,
    max_chunk_chars: int = DEFAULT_MAX_CHARS,
    chunker_version: str = CHUNKER_VERSION,
) -> IngestResult:
    """Produce canonical + chunks + durable blob outputs (idempotent overwrite)."""
    try:
        raw = storage.get(artifact_key)
    except FileNotFoundError as exc:
        raise DeterministicIngestError(
            "missing_artifact", f"Artifact blob missing: {artifact_key}"
        ) from exc

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

    canonical = normalize_to_canonical(text)
    if chunker_version != CHUNKER_VERSION:
        # Explicit non-default versions are for experiments; production path
        # always stamps CHUNKER_VERSION. Unknown versions still run hybrid.
        pass
    chunks = chunk_hybrid(canonical, max_chars=max_chunk_chars)

    ckey = canonical_key_for(job_id)
    incoming_c = storage.incoming_root() / f"ingest-canonical-{job_id}"
    incoming_c.write_text(canonical.text, encoding="utf-8")
    storage.put_from_path(ckey, incoming_c, "text/plain; charset=utf-8")

    payload = {
        "stage": "CHUNKED",
        "job_id": str(job_id),
        "artifact_key": artifact_key,
        "parser_version": PARSER_VERSION,
        "chunker_version": chunker_version,
        "content_kind": canonical.kind.value,
        "canonical_key": ckey,
        "canonical_chars": canonical.char_count,
        "canonical_sha256": canonical.content_sha256,
        "canonical_line_count": canonical.line_count,
        "chunk_count": len(chunks),
        "max_chunk_chars": max_chunk_chars,
        "attempt_count": attempt_count,
        "chunk_ordinals": [c.ordinal for c in chunks],
        "chunk_content_sha256": [c.content_sha256 for c in chunks],
    }
    rkey = result_key_for(job_id)
    incoming_r = storage.incoming_root() / f"ingest-result-{job_id}"
    incoming_r.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    storage.put_from_path(rkey, incoming_r, "application/json")

    return IngestResult(
        result_key=rkey,
        canonical_key=ckey,
        parser_version=PARSER_VERSION,
        chunker_version=chunker_version,
        canonical_chars=canonical.char_count,
        canonical_sha256=canonical.content_sha256,
        content_kind=canonical.kind.value,
        chunk_count=len(chunks),
        max_chunk_chars=max_chunk_chars,
        chunks=tuple(chunks),
    )


def cleanup_partial_result(storage: ArtifactStorage, job_id: UUID) -> None:
    """Best-effort remove incomplete ingestion blob outputs."""
    for key in (result_key_for(job_id), canonical_key_for(job_id)):
        try:
            storage.delete(key)
        except Exception:
            pass
    for name in (f"ingest-result-{job_id}", f"ingest-canonical-{job_id}", f"ingest-{job_id}"):
        try:
            path = storage.incoming_root() / name
            if path.is_file():
                path.unlink()
        except Exception:
            pass

"""Lexical retrieval contract and PostgreSQL full-text implementations.

Versions:

- ``cf-lex-fts-1`` — english FTS + ``websearch_to_tsquery`` + ``ts_rank_cd``
- ``cf-lex-fts-simple-1`` — ``simple`` dictionary (better identifiers / no stem)
- ``cf-lex-fts-struct-1`` — english FTS + bounded heading/identifier boosts

PostgreSQL ``ts_rank_cd`` is **not** claimed to be BM25.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RETRIEVAL_VERSION_FTS = "cf-lex-fts-1"
RETRIEVAL_VERSION_SIMPLE = "cf-lex-fts-simple-1"
RETRIEVAL_VERSION_STRUCT = "cf-lex-fts-struct-1"


class RetrievalVersion(StrEnum):
    FTS = RETRIEVAL_VERSION_FTS
    SIMPLE = RETRIEVAL_VERSION_SIMPLE
    STRUCT = RETRIEVAL_VERSION_STRUCT


@dataclass(frozen=True)
class RetrievalFilters:
    """Optional scope filters. Future tenant/challenge isolation plugs in here."""

    submission_id: UUID | None = None
    artifact_key: str | None = None
    ingestion_job_id: UUID | None = None


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: UUID
    ingestion_job_id: UUID
    submission_id: UUID
    artifact_key: str
    ordinal: int
    content: str
    score: float
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    block_type: str
    heading_path: tuple[str, ...]
    parser_version: str
    chunker_version: str
    retrieval_version: str
    content_sha256: str


def normalize_query(query: str) -> str:
    """Light query normalization — preserve identifiers and phrases."""
    return " ".join((query or "").strip().split())


async def retrieve(
    session: AsyncSession,
    query: str,
    *,
    top_k: int = 10,
    filters: RetrievalFilters | None = None,
    retrieval_version: str = RETRIEVAL_VERSION_FTS,
) -> list[RetrievedChunk]:
    """Retrieve top-k chunks from succeeded ingestion jobs only.

    Ordering: score DESC, chunk_id ASC (deterministic ties).
    """
    if top_k < 1:
        raise ValueError("top_k must be >= 1")
    q = normalize_query(query)
    if not q:
        return []

    filters = filters or RetrievalFilters()
    version = retrieval_version

    if version == RETRIEVAL_VERSION_SIMPLE:
        return await _retrieve_simple(session, q, top_k=top_k, filters=filters)
    if version == RETRIEVAL_VERSION_STRUCT:
        return await _retrieve_struct(session, q, top_k=top_k, filters=filters)
    if version == RETRIEVAL_VERSION_FTS:
        return await _retrieve_fts(session, q, top_k=top_k, filters=filters)
    raise ValueError(f"Unknown retrieval_version: {retrieval_version}")


def _filter_clauses(filters: RetrievalFilters, params: dict[str, Any]) -> str:
    parts: list[str] = []
    if filters.submission_id is not None:
        parts.append("AND c.submission_id = :submission_id")
        params["submission_id"] = filters.submission_id
    if filters.artifact_key is not None:
        parts.append("AND c.artifact_key = :artifact_key")
        params["artifact_key"] = filters.artifact_key
    if filters.ingestion_job_id is not None:
        parts.append("AND c.ingestion_job_id = :ingestion_job_id")
        params["ingestion_job_id"] = filters.ingestion_job_id
    return "\n".join(parts)


def _rows_to_results(
    rows: Sequence[Any], *, retrieval_version: str
) -> list[RetrievedChunk]:
    out: list[RetrievedChunk] = []
    for row in rows:
        path = row.heading_path or []
        out.append(
            RetrievedChunk(
                chunk_id=row.id,
                ingestion_job_id=row.ingestion_job_id,
                submission_id=row.submission_id,
                artifact_key=row.artifact_key,
                ordinal=int(row.ordinal),
                content=row.content,
                score=float(row.score),
                char_start=int(row.char_start),
                char_end=int(row.char_end),
                line_start=int(row.line_start),
                line_end=int(row.line_end),
                block_type=row.block_type,
                heading_path=tuple(str(x) for x in path),
                parser_version=row.parser_version,
                chunker_version=row.chunker_version,
                retrieval_version=retrieval_version,
                content_sha256=row.content_sha256,
            )
        )
    return out


async def _retrieve_fts(
    session: AsyncSession,
    query: str,
    *,
    top_k: int,
    filters: RetrievalFilters,
) -> list[RetrievedChunk]:
    params: dict[str, Any] = {"q": query, "top_k": top_k}
    extra = _filter_clauses(filters, params)
    sql = text(
        f"""
        WITH qq AS (
            SELECT websearch_to_tsquery('english', :q) AS tsq
        )
        SELECT
            c.id,
            c.ingestion_job_id,
            c.submission_id,
            c.artifact_key,
            c.ordinal,
            c.content,
            c.char_start,
            c.char_end,
            c.line_start,
            c.line_end,
            c.block_type,
            c.heading_path,
            c.parser_version,
            c.chunker_version,
            c.content_sha256,
            ts_rank_cd(c.search_tsv, qq.tsq) AS score
        FROM document_chunks c
        JOIN ingestion_jobs j ON j.id = c.ingestion_job_id
        CROSS JOIN qq
        WHERE j.status = 'succeeded'
          AND c.search_tsv @@ qq.tsq
          {extra}
        ORDER BY score DESC, c.id ASC
        LIMIT :top_k
        """
    )
    result = await session.execute(sql, params)
    return _rows_to_results(result.fetchall(), retrieval_version=RETRIEVAL_VERSION_FTS)


async def _retrieve_simple(
    session: AsyncSession,
    query: str,
    *,
    top_k: int,
    filters: RetrievalFilters,
) -> list[RetrievedChunk]:
    params: dict[str, Any] = {"q": query, "top_k": top_k}
    extra = _filter_clauses(filters, params)
    sql = text(
        f"""
        WITH qq AS (
            SELECT plainto_tsquery('simple', :q) AS tsq
        )
        SELECT
            c.id,
            c.ingestion_job_id,
            c.submission_id,
            c.artifact_key,
            c.ordinal,
            c.content,
            c.char_start,
            c.char_end,
            c.line_start,
            c.line_end,
            c.block_type,
            c.heading_path,
            c.parser_version,
            c.chunker_version,
            c.content_sha256,
            ts_rank_cd(c.search_tsv_simple, qq.tsq) AS score
        FROM document_chunks c
        JOIN ingestion_jobs j ON j.id = c.ingestion_job_id
        CROSS JOIN qq
        WHERE j.status = 'succeeded'
          AND c.search_tsv_simple @@ qq.tsq
          {extra}
        ORDER BY score DESC, c.id ASC
        LIMIT :top_k
        """
    )
    result = await session.execute(sql, params)
    return _rows_to_results(
        result.fetchall(), retrieval_version=RETRIEVAL_VERSION_SIMPLE
    )


async def _retrieve_struct(
    session: AsyncSession,
    query: str,
    *,
    top_k: int,
    filters: RetrievalFilters,
) -> list[RetrievedChunk]:
    """Lexical score + bounded structural boosts (heading match, exact ILIKE)."""
    params: dict[str, Any] = {"q": query, "top_k": top_k}
    extra = _filter_clauses(filters, params)
    # Boosts are intentionally small and explicit (versioned policy).
    sql = text(
        f"""
        WITH qq AS (
            SELECT websearch_to_tsquery('english', :q) AS tsq
        )
        SELECT
            c.id,
            c.ingestion_job_id,
            c.submission_id,
            c.artifact_key,
            c.ordinal,
            c.content,
            c.char_start,
            c.char_end,
            c.line_start,
            c.line_end,
            c.block_type,
            c.heading_path,
            c.parser_version,
            c.chunker_version,
            c.content_sha256,
            (
                ts_rank_cd(c.search_tsv, qq.tsq)
                + CASE
                    WHEN c.heading_path::text ILIKE ('%%' || :q || '%%') THEN 0.15
                    ELSE 0
                  END
                + CASE
                    WHEN c.content ILIKE ('%%' || :q || '%%') THEN 0.10
                    ELSE 0
                  END
                + CASE
                    WHEN c.block_type IN ('code', 'code_fence')
                         AND c.content ILIKE ('%%' || :q || '%%') THEN 0.05
                    ELSE 0
                  END
            ) AS score
        FROM document_chunks c
        JOIN ingestion_jobs j ON j.id = c.ingestion_job_id
        CROSS JOIN qq
        WHERE j.status = 'succeeded'
          AND (
              c.search_tsv @@ qq.tsq
              OR c.content ILIKE ('%%' || :q || '%%')
              OR c.heading_path::text ILIKE ('%%' || :q || '%%')
          )
          {extra}
        ORDER BY score DESC, c.id ASC
        LIMIT :top_k
        """
    )
    result = await session.execute(sql, params)
    return _rows_to_results(
        result.fetchall(), retrieval_version=RETRIEVAL_VERSION_STRUCT
    )

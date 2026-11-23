"""Dense and hybrid (RRF) retrieval over chunk_embeddings.

Versions:

- ``cf-dense-1`` — cosine KNN on L2-normalized vectors for one embedding_version
- ``cf-hybrid-rrf-1`` — Reciprocal Rank Fusion of lexical + dense

Vectors are stored as ``double precision[]``. Scoring uses in-process cosine
(equivalent to dot product when L2-normalized) so CI works without pgvector.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from challengeforge.retrieval.embeddings import EmbeddingProvider, cosine_similarity
from challengeforge.retrieval.lexical import (
    RETRIEVAL_VERSION_FTS,
    RetrievedChunk,
    RetrievalFilters,
    _filter_clauses,
    normalize_query,
    retrieve as lexical_retrieve,
)

RETRIEVAL_VERSION_DENSE = "cf-dense-1"
RETRIEVAL_VERSION_HYBRID_RRF = "cf-hybrid-rrf-1"
RRF_K = 60


def rrf_fuse(
    ranked_lists: list[list[UUID]],
    *,
    k: int = RRF_K,
) -> list[tuple[UUID, float]]:
    scores: dict[UUID, float] = {}
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))


async def index_chunks(
    session: AsyncSession,
    provider: EmbeddingProvider,
    chunks: list[Any],
) -> int:
    from challengeforge.persistence.repositories import ChunkEmbeddingRepository

    if not chunks:
        return 0
    texts = [c.content for c in chunks]
    vectors = provider.embed_documents(texts)
    items = [(c.id, vectors[i], c.content_sha256) for i, c in enumerate(chunks)]
    return await ChunkEmbeddingRepository(session).upsert_many(
        embedding_version=provider.embedding_version,
        items=items,
    )


def _row_to_chunk(row: Any, *, score: float, retrieval_version: str) -> RetrievedChunk:
    path = row.heading_path or []
    return RetrievedChunk(
        chunk_id=row.id,
        ingestion_job_id=row.ingestion_job_id,
        submission_id=row.submission_id,
        artifact_key=row.artifact_key,
        ordinal=int(row.ordinal),
        content=row.content,
        score=float(score),
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


async def retrieve_dense(
    session: AsyncSession,
    query: str,
    *,
    provider: EmbeddingProvider,
    top_k: int = 10,
    filters: RetrievalFilters | None = None,
) -> list[RetrievedChunk]:
    q = normalize_query(query)
    if not q or top_k < 1:
        return []
    filters = filters or RetrievalFilters()
    qvec = provider.embed_query(q)
    params: dict[str, Any] = {
        "embedding_version": provider.embedding_version,
        "dim": len(qvec),
    }
    extra = _filter_clauses(filters, params)
    sql = text(
        f"""
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
            e.embedding AS embedding
        FROM document_chunks c
        JOIN ingestion_jobs j ON j.id = c.ingestion_job_id
        JOIN chunk_embeddings e
          ON e.chunk_id = c.id
         AND e.embedding_version = :embedding_version
        WHERE j.status = 'succeeded'
          AND e.dimensions = :dim
          {extra}
        """
    )
    rows = (await session.execute(sql, params)).fetchall()
    scored: list[RetrievedChunk] = []
    for row in rows:
        emb = list(row.embedding)
        if len(emb) != len(qvec):
            continue
        score = cosine_similarity(qvec, emb)
        scored.append(
            _row_to_chunk(
                row, score=score, retrieval_version=RETRIEVAL_VERSION_DENSE
            )
        )
    scored.sort(key=lambda h: (-h.score, str(h.chunk_id)))
    return scored[:top_k]


async def retrieve_hybrid_rrf(
    session: AsyncSession,
    query: str,
    *,
    provider: EmbeddingProvider,
    top_k: int = 10,
    candidate_k: int = 20,
    filters: RetrievalFilters | None = None,
    rrf_k: int = RRF_K,
) -> list[RetrievedChunk]:
    q = normalize_query(query)
    if not q or top_k < 1:
        return []
    filters = filters or RetrievalFilters()
    n = max(candidate_k, top_k)
    lex = await lexical_retrieve(
        session,
        q,
        top_k=n,
        filters=filters,
        retrieval_version=RETRIEVAL_VERSION_FTS,
    )
    dense = await retrieve_dense(
        session, q, provider=provider, top_k=n, filters=filters
    )
    fused = rrf_fuse(
        [[h.chunk_id for h in lex], [h.chunk_id for h in dense]],
        k=rrf_k,
    )[:top_k]
    by_id = {h.chunk_id: h for h in (*lex, *dense)}
    out: list[RetrievedChunk] = []
    for chunk_id, score in fused:
        base = by_id.get(chunk_id)
        if base is None:
            continue
        out.append(
            RetrievedChunk(
                chunk_id=base.chunk_id,
                ingestion_job_id=base.ingestion_job_id,
                submission_id=base.submission_id,
                artifact_key=base.artifact_key,
                ordinal=base.ordinal,
                content=base.content,
                score=float(score),
                char_start=base.char_start,
                char_end=base.char_end,
                line_start=base.line_start,
                line_end=base.line_end,
                block_type=base.block_type,
                heading_path=base.heading_path,
                parser_version=base.parser_version,
                chunker_version=base.chunker_version,
                retrieval_version=RETRIEVAL_VERSION_HYBRID_RRF,
                content_sha256=base.content_sha256,
            )
        )
    return out

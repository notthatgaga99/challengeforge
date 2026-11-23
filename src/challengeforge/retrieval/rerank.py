"""Cross-encoder-style rerankers over hybrid candidate lists.

``cf-hybrid-rerank-1`` = hybrid RRF shortlist → pairwise rescore → top_k.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from challengeforge.retrieval.dense import (
    RETRIEVAL_VERSION_HYBRID_RRF,
    retrieve_hybrid_rrf,
)
from challengeforge.retrieval.embeddings import EmbeddingProvider
from challengeforge.retrieval.lexical import RetrievedChunk, RetrievalFilters, normalize_query

RETRIEVAL_VERSION_HYBRID_RERANK = "cf-hybrid-rerank-1"
DEFAULT_CANDIDATE_K = 20

_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)


class RerankProvider(ABC):
    @property
    @abstractmethod
    def rerank_version(self) -> str:
        """Identifier for provenance / experiment metadata."""

    @abstractmethod
    def score_pairs(self, query: str, documents: Sequence[str]) -> list[float]:
        """Higher is more relevant. Length must match ``documents``."""


class LexicalOverlapRerankProvider(RerankProvider):
    """Educational pairwise scorer — query-term coverage + shared tokens.

    Not a neural cross-encoder. Demonstrates the *stage* (candidates → rescore)
    without requiring GPU models in CI.
    """

    @property
    def rerank_version(self) -> str:
        return "curriculum:lexical-overlap-v1"

    def score_pairs(self, query: str, documents: Sequence[str]) -> list[float]:
        q_toks = set(_TOKEN_RE.findall((query or "").lower()))
        if not q_toks:
            return [0.0] * len(documents)
        scores: list[float] = []
        for doc in documents:
            d_toks = set(_TOKEN_RE.findall((doc or "").lower()))
            if not d_toks:
                scores.append(0.0)
                continue
            inter = len(q_toks & d_toks)
            # Query coverage + Jaccard — deterministic, bounded [0, 2].
            coverage = inter / len(q_toks)
            jaccard = inter / len(q_toks | d_toks)
            scores.append(coverage + jaccard)
        return scores


class LocalCrossEncoderRerankProvider(RerankProvider):
    """Optional neural cross-encoder when sentence-transformers is installed."""

    MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def __init__(self) -> None:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is not installed; use LexicalOverlapRerankProvider"
            ) from exc
        self._model = CrossEncoder(self.MODEL_NAME)

    @property
    def rerank_version(self) -> str:
        return f"local:ms-marco-MiniLM-L-6-v2"

    def score_pairs(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        pairs = [(query, d) for d in documents]
        raw = self._model.predict(pairs, show_progress_bar=False)
        return [float(x) for x in raw]


def default_rerank_provider() -> RerankProvider:
    try:
        return LocalCrossEncoderRerankProvider()
    except RuntimeError:
        return LexicalOverlapRerankProvider()


def rerank_hits(
    query: str,
    hits: Sequence[RetrievedChunk],
    reranker: RerankProvider,
    *,
    top_k: int,
    retrieval_version: str = RETRIEVAL_VERSION_HYBRID_RERANK,
) -> list[RetrievedChunk]:
    if not hits or top_k < 1:
        return []
    scores = reranker.score_pairs(query, [h.content for h in hits])
    if len(scores) != len(hits):
        raise ValueError("reranker returned unexpected score count")
    ranked = sorted(
        zip(hits, scores, strict=True),
        key=lambda pair: (-pair[1], str(pair[0].chunk_id)),
    )[:top_k]
    out: list[RetrievedChunk] = []
    for base, score in ranked:
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
                retrieval_version=retrieval_version,
                content_sha256=base.content_sha256,
            )
        )
    return out


async def retrieve_hybrid_rerank(
    session: AsyncSession,
    query: str,
    *,
    provider: EmbeddingProvider,
    reranker: RerankProvider,
    top_k: int = 10,
    candidate_k: int = DEFAULT_CANDIDATE_K,
    filters: RetrievalFilters | None = None,
) -> list[RetrievedChunk]:
    q = normalize_query(query)
    if not q or top_k < 1:
        return []
    n = max(candidate_k, top_k)
    candidates = await retrieve_hybrid_rrf(
        session,
        q,
        provider=provider,
        top_k=n,
        candidate_k=n,
        filters=filters,
    )
    # Tag intermediate hybrid scores are replaced by rerank scores.
    return rerank_hits(
        q,
        candidates,
        reranker,
        top_k=top_k,
        retrieval_version=RETRIEVAL_VERSION_HYBRID_RERANK,
    )

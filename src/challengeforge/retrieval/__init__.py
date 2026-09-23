"""Retrieval package — lexical baseline over durable document_chunks."""

from challengeforge.retrieval.lexical import (
    RETRIEVAL_VERSION_FTS,
    RETRIEVAL_VERSION_SIMPLE,
    RETRIEVAL_VERSION_STRUCT,
    RetrievedChunk,
    RetrievalFilters,
    normalize_query,
    retrieve,
)
from challengeforge.retrieval.metrics import (
    evaluate_query,
    macro_average,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

__all__ = [
    "RETRIEVAL_VERSION_FTS",
    "RETRIEVAL_VERSION_SIMPLE",
    "RETRIEVAL_VERSION_STRUCT",
    "RetrievedChunk",
    "RetrievalFilters",
    "normalize_query",
    "retrieve",
    "evaluate_query",
    "macro_average",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]

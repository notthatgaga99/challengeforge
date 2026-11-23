"""Retrieval package — lexical baseline + dense/hybrid semantic retrieval."""

from challengeforge.retrieval.authz import resolve_retrieval_scope
from challengeforge.retrieval.dense import (
    RETRIEVAL_VERSION_DENSE,
    RETRIEVAL_VERSION_HYBRID_RRF,
    index_chunks,
    retrieve_dense,
    retrieve_hybrid_rrf,
    rrf_fuse,
)
from challengeforge.retrieval.rerank import (
    RETRIEVAL_VERSION_HYBRID_RERANK,
    LexicalOverlapRerankProvider,
    LocalCrossEncoderRerankProvider,
    default_rerank_provider,
    rerank_hits,
    retrieve_hybrid_rerank,
)
from challengeforge.retrieval.embeddings import (
    CurriculumConceptEmbeddingProvider,
    EmbeddingProvider,
    LocalMiniLMEmbeddingProvider,
    StubHashEmbeddingProvider,
    cosine_similarity,
    default_experiment_provider,
    resolve_product_embedding_provider,
    version_requires_embeddings,
)
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
    "RETRIEVAL_VERSION_DENSE",
    "RETRIEVAL_VERSION_HYBRID_RRF",
    "RETRIEVAL_VERSION_HYBRID_RERANK",
    "RetrievedChunk",
    "RetrievalFilters",
    "EmbeddingProvider",
    "StubHashEmbeddingProvider",
    "CurriculumConceptEmbeddingProvider",
    "LocalMiniLMEmbeddingProvider",
    "LexicalOverlapRerankProvider",
    "LocalCrossEncoderRerankProvider",
    "cosine_similarity",
    "default_experiment_provider",
    "default_rerank_provider",
    "resolve_product_embedding_provider",
    "version_requires_embeddings",
    "resolve_retrieval_scope",
    "normalize_query",
    "retrieve",
    "retrieve_dense",
    "retrieve_hybrid_rrf",
    "retrieve_hybrid_rerank",
    "rerank_hits",
    "index_chunks",
    "rrf_fuse",
    "evaluate_query",
    "macro_average",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]

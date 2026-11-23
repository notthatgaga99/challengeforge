"""Embedding providers for dense retrieval.

``embedding_version`` format: ``<provider>:<model>:<dim>:<metric>``

- ``stub:cf-hash-v1`` — deterministic CI/pipeline only (not semantic proof)
- ``curriculum:concept-v1`` — lightweight concept-space encoder that demonstrates
  paraphrase geometry on ChallengeForge jargon without a large model download
- ``local:all-MiniLM-L6-v2`` — optional real bi-encoder when sentence-transformers
  is installed
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod


_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)


class EmbeddingProvider(ABC):
    @property
    @abstractmethod
    def embedding_version(self) -> str: ...

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0.0:
        return vec
    return [x / norm for x in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("vector length mismatch")
    return sum(x * y for x, y in zip(a, b, strict=True))


class StubHashEmbeddingProvider(EmbeddingProvider):
    """Deterministic hash embedding — teaches the pipeline, not semantics."""

    def __init__(self, *, dimensions: int = 64) -> None:
        if dimensions < 8:
            raise ValueError("dimensions must be >= 8")
        self._dim = dimensions

    @property
    def embedding_version(self) -> str:
        return f"stub:cf-hash-v1:{self._dim}:cosine"

    @property
    def dimensions(self) -> int:
        return self._dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for tok in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(tok.encode("utf-8")).digest()
            for i in range(0, min(len(digest), self._dim)):
                # map byte to [-1, 1]
                vec[i % self._dim] += (digest[i] / 127.5) - 1.0
        return _l2_normalize(vec)


# Concept clusters: tokens that should land near each other in curriculum space.
# This is an *educational* stand-in for a neural bi-encoder on our domain jargon.
_CONCEPT_CLUSTERS: list[tuple[str, frozenset[str]]] = [
    (
        "worker_recovery",
        frozenset(
            {
                "worker",
                "crash",
                "crashes",
                "crashed",
                "failure",
                "recover",
                "recovery",
                "requeued",
                "requeue",
                "stale",
                "running",
                "disappears",
                "orphan",
            }
        ),
    ),
    (
        "claim_concurrency",
        frozenset(
            {
                "claim",
                "claiming",
                "grab",
                "skip",
                "locked",
                "blocking",
                "block",
                "concurrent",
                "workers",
                "job",
                "jobs",
                "queue",
            }
        ),
    ),
    (
        "idempotency",
        frozenset({"idempotent", "idempotency", "retry", "retries", "overwrite"}),
    ),
    (
        "transactions",
        frozenset(
            {"transaction", "transactions", "serialization", "database", "retries"}
        ),
    ),
    (
        "upload",
        frozenset({"upload", "streaming", "finalize", "incoming", "artifact", "multipart"}),
    ),
]


class CurriculumConceptEmbeddingProvider(EmbeddingProvider):
    """Domain concept embedding for learning semantic retrieval effects.

    Not a substitute for MiniLM/E5 in production. It *does* place paraphrase
    vocabulary for ChallengeForge recovery/claim concepts nearby so dense
    retrieval can beat pure lexical FTS on designed paraphrase queries.
    """

    def __init__(self, *, dimensions: int = 128) -> None:
        if dimensions < 32:
            raise ValueError("dimensions must be >= 32")
        self._dim = dimensions
        self._cluster_bases = {
            name: self._cluster_base(name) for name, _ in _CONCEPT_CLUSTERS
        }

    @property
    def embedding_version(self) -> str:
        return f"curriculum:concept-v1:{self._dim}:cosine"

    @property
    def dimensions(self) -> int:
        return self._dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _cluster_base(self, name: str) -> list[float]:
        digest = hashlib.sha256(f"cluster:{name}".encode()).digest()
        raw = [((digest[i % len(digest)] / 127.5) - 1.0) for i in range(self._dim)]
        return _l2_normalize(raw)

    def _one(self, text: str) -> list[float]:
        tokens = set(_TOKEN_RE.findall(text.lower()))
        vec = [0.0] * self._dim
        # Concept contributions
        for name, vocab in _CONCEPT_CLUSTERS:
            overlap = len(tokens & vocab)
            if overlap:
                base = self._cluster_bases[name]
                weight = float(overlap)
                for i, v in enumerate(base):
                    vec[i] += weight * v
        # Residual token hashing so exact rare terms still have signal
        for tok in tokens:
            digest = hashlib.sha256(f"tok:{tok}".encode()).digest()
            idx = digest[0] % self._dim
            vec[idx] += 0.35
            vec[(idx + 1) % self._dim] += (digest[1] / 255.0) * 0.2
        if all(x == 0.0 for x in vec):
            return _l2_normalize(StubHashEmbeddingProvider(dimensions=self._dim)._one(text))
        return _l2_normalize(vec)


class LocalMiniLMEmbeddingProvider(EmbeddingProvider):
    """Optional real bi-encoder when ``sentence_transformers`` is installed."""

    MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
    DIM = 384

    def __init__(self) -> None:
        import os

        # Progress bars + tokenizers fork warnings can deadlock under pytest on Windows.
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is not installed; use curriculum or stub provider"
            ) from exc
        self._model = SentenceTransformer(self.MODEL_NAME)

    @property
    def embedding_version(self) -> str:
        return f"local:all-MiniLM-L6-v2:{self.DIM}:cosine"

    @property
    def dimensions(self) -> int:
        return self.DIM

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        )
        return [list(map(float, row)) for row in vectors]


def default_experiment_provider() -> EmbeddingProvider:
    """Prefer MiniLM if present; else curriculum concept encoder."""
    try:
        return LocalMiniLMEmbeddingProvider()
    except RuntimeError:
        return CurriculumConceptEmbeddingProvider()


def resolve_product_embedding_provider(name: str = "curriculum") -> EmbeddingProvider:
    """Resolve the product-path encoder from settings (ADR 0018)."""
    key = (name or "curriculum").strip().lower()
    if key == "stub":
        return StubHashEmbeddingProvider()
    if key == "minilm":
        return LocalMiniLMEmbeddingProvider()
    if key == "curriculum":
        return CurriculumConceptEmbeddingProvider()
    raise ValueError(f"Unknown retrieval_embedding_provider: {name}")


def version_requires_embeddings(retrieval_version: str) -> bool:
    from challengeforge.retrieval.dense import (
        RETRIEVAL_VERSION_DENSE,
        RETRIEVAL_VERSION_HYBRID_RRF,
    )
    from challengeforge.retrieval.rerank import RETRIEVAL_VERSION_HYBRID_RERANK

    return retrieval_version in (
        RETRIEVAL_VERSION_DENSE,
        RETRIEVAL_VERSION_HYBRID_RRF,
        RETRIEVAL_VERSION_HYBRID_RERANK,
    )

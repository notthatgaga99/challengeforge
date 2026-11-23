"""Product search over succeeded ingestion chunks with tenancy authz."""

from __future__ import annotations

from uuid import UUID

from challengeforge.application import UnitOfWork, require_non_empty
from challengeforge.application.rate_limits import ACTION_SEARCH, RateLimitService
from challengeforge.domain.exceptions import NotFoundError, ValidationFailed
from challengeforge.identity import CurrentUser
from challengeforge.retrieval.authz import resolve_retrieval_scope
from challengeforge.retrieval.embeddings import (
    resolve_product_embedding_provider,
    version_requires_embeddings,
)
from challengeforge.retrieval.lexical import RetrievedChunk, retrieve


class SearchService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    def resolve_retrieval_version(self, requested: str | None) -> str:
        """Client override wins; otherwise settings default (ADR 0018)."""
        if requested is not None and requested.strip():
            return requested.strip()
        return self.uow.settings.retrieval_default_version

    async def search_hackathon(
        self,
        actor: CurrentUser,
        hackathon_id: UUID,
        *,
        query: str,
        top_k: int | None = None,
        challenge_id: UUID | None = None,
        retrieval_version: str | None = None,
    ) -> tuple[list[RetrievedChunk], str]:
        """Return (hits, effective_retrieval_version)."""
        query = require_non_empty(query, "query")
        effective_top_k = (
            top_k if top_k is not None else self.uow.settings.retrieval_default_top_k
        )
        if effective_top_k < 1 or effective_top_k > 50:
            raise ValidationFailed("top_k must be between 1 and 50.")

        await RateLimitService(self.uow).check(actor, ACTION_SEARCH)

        version = self.resolve_retrieval_version(retrieval_version)

        hackathon = await self.uow.hackathons.get(hackathon_id)
        if hackathon is None:
            raise NotFoundError("Hackathon not found.")

        challenge_hackathon_id: UUID | None = None
        if challenge_id is not None:
            challenge = await self.uow.challenges.get(challenge_id)
            if challenge is None:
                raise NotFoundError("Challenge not found.")
            challenge_hackathon_id = challenge.hackathon_id

        filters = resolve_retrieval_scope(
            actor,
            hackathon,
            challenge_id=challenge_id,
            challenge_hackathon_id=challenge_hackathon_id,
        )

        provider = None
        reranker = None
        if version_requires_embeddings(version):
            try:
                provider = resolve_product_embedding_provider(
                    self.uow.settings.retrieval_embedding_provider
                )
            except RuntimeError as exc:
                raise ValidationFailed(
                    f"Embedding provider unavailable for {version}: {exc}"
                ) from exc
            from challengeforge.retrieval.rerank import (
                RETRIEVAL_VERSION_HYBRID_RERANK,
                LexicalOverlapRerankProvider,
            )

            if version == RETRIEVAL_VERSION_HYBRID_RERANK:
                reranker = LexicalOverlapRerankProvider()

        hits = await retrieve(
            self.uow.session,
            query,
            top_k=effective_top_k,
            filters=filters,
            retrieval_version=version,
            embedding_provider=provider,
            rerank_provider=reranker,
        )
        return hits, version

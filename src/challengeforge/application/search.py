"""Product search over succeeded ingestion chunks with tenancy authz."""

from __future__ import annotations

from uuid import UUID

from challengeforge.application import UnitOfWork, require_non_empty
from challengeforge.domain.exceptions import NotFoundError, ValidationFailed
from challengeforge.identity import CurrentUser
from challengeforge.retrieval.authz import resolve_retrieval_scope
from challengeforge.retrieval.lexical import (
    RETRIEVAL_VERSION_FTS,
    RetrievedChunk,
    retrieve,
)


class SearchService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def search_hackathon(
        self,
        actor: CurrentUser,
        hackathon_id: UUID,
        *,
        query: str,
        top_k: int = 10,
        challenge_id: UUID | None = None,
        retrieval_version: str = RETRIEVAL_VERSION_FTS,
    ) -> list[RetrievedChunk]:
        query = require_non_empty(query, "query")
        if top_k < 1 or top_k > 50:
            raise ValidationFailed("top_k must be between 1 and 50.")

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
        return await retrieve(
            self.uow.session,
            query,
            top_k=top_k,
            filters=filters,
            retrieval_version=retrieval_version,
        )

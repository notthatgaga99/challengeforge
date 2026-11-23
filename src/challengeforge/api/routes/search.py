"""Hackathon-scoped search API (tenancy enforced)."""

from uuid import UUID

from fastapi import APIRouter, Depends

from challengeforge.api.deps import get_current_user, search_service
from challengeforge.api.schemas import SearchHitResponse, SearchRequest, SearchResponse
from challengeforge.application.search import SearchService
from challengeforge.identity import CurrentUser

router = APIRouter()


@router.post(
    "/hackathons/{hackathon_id}/search",
    response_model=SearchResponse,
)
async def search_hackathon(
    hackathon_id: UUID,
    payload: SearchRequest,
    actor: CurrentUser = Depends(get_current_user),
    service: SearchService = Depends(search_service),
) -> SearchResponse:
    hits, effective_version = await service.search_hackathon(
        actor,
        hackathon_id,
        query=payload.query,
        top_k=payload.top_k,
        challenge_id=payload.challenge_id,
        retrieval_version=payload.retrieval_version,
    )
    return SearchResponse(
        hackathon_id=hackathon_id,
        query=payload.query,
        retrieval_version=effective_version,
        hits=[
            SearchHitResponse(
                chunk_id=h.chunk_id,
                submission_id=h.submission_id,
                artifact_key=h.artifact_key,
                score=h.score,
                content=h.content,
                char_start=h.char_start,
                char_end=h.char_end,
                line_start=h.line_start,
                line_end=h.line_end,
                block_type=h.block_type,
                heading_path=list(h.heading_path),
                retrieval_version=h.retrieval_version,
                content_sha256=h.content_sha256,
            )
            for h in hits
        ],
    )

from uuid import UUID

from fastapi import APIRouter, Depends, status

from challengeforge.api.deps import challenge_service, get_current_user, hackathon_service
from challengeforge.api.schemas import (
    ChallengeCreateRequest,
    ChallengeResponse,
    ChallengeSpecificationResponse,
    EvaluationCriterionResponse,
    HackathonCreateRequest,
    HackathonResponse,
)
from challengeforge.application.challenges import ChallengeService
from challengeforge.application.hackathons import HackathonService
from challengeforge.domain.models import Challenge, Hackathon
from challengeforge.identity import CurrentUser

router = APIRouter()


def serialize_hackathon(item: Hackathon) -> HackathonResponse:
    return HackathonResponse(
        id=item.id,
        title=item.title,
        description=item.description,
        status=item.status.value,
        organizer_id=item.organizer_id,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def serialize_challenge(item: Challenge) -> ChallengeResponse:
    return ChallengeResponse(
        id=item.id,
        hackathon_id=item.hackathon_id,
        title=item.title,
        description=item.description,
        constraints=item.constraints,
        status=item.status.value,
        specification=ChallengeSpecificationResponse(
            body=item.specification.body,
            created_at=item.specification.created_at,
            updated_at=item.specification.updated_at,
        ),
        evaluation_criteria=[
            EvaluationCriterionResponse(
                id=c.id, name=c.name, description=c.description, weight=c.weight
            )
            for c in item.evaluation_criteria
        ],
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("/hackathons", response_model=list[HackathonResponse])
async def list_hackathons(
    actor: CurrentUser = Depends(get_current_user),
    service: HackathonService = Depends(hackathon_service),
) -> list[HackathonResponse]:
    items = await service.list_hackathons(actor)
    return [serialize_hackathon(item) for item in items]


@router.post(
    "/hackathons",
    response_model=HackathonResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_hackathon(
    payload: HackathonCreateRequest,
    actor: CurrentUser = Depends(get_current_user),
    service: HackathonService = Depends(hackathon_service),
) -> HackathonResponse:
    item = await service.create_hackathon(actor, payload.title, payload.description)
    return serialize_hackathon(item)


@router.get("/hackathons/{hackathon_id}", response_model=HackathonResponse)
async def get_hackathon(
    hackathon_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: HackathonService = Depends(hackathon_service),
) -> HackathonResponse:
    item = await service.get_hackathon(actor, hackathon_id)
    return serialize_hackathon(item)


@router.post("/hackathons/{hackathon_id}/publish", response_model=HackathonResponse)
async def publish_hackathon(
    hackathon_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: HackathonService = Depends(hackathon_service),
) -> HackathonResponse:
    item = await service.publish_hackathon(actor, hackathon_id)
    return serialize_hackathon(item)


@router.get("/hackathons/{hackathon_id}/challenges", response_model=list[ChallengeResponse])
async def list_challenges(
    hackathon_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: ChallengeService = Depends(challenge_service),
) -> list[ChallengeResponse]:
    items = await service.list_for_hackathon(actor, hackathon_id)
    return [serialize_challenge(item) for item in items]


@router.post(
    "/hackathons/{hackathon_id}/challenges",
    response_model=ChallengeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_challenge(
    hackathon_id: UUID,
    payload: ChallengeCreateRequest,
    actor: CurrentUser = Depends(get_current_user),
    service: ChallengeService = Depends(challenge_service),
) -> ChallengeResponse:
    item = await service.create_challenge(
        actor,
        hackathon_id,
        title=payload.title,
        description=payload.description,
        constraints=payload.constraints,
        specification_body=payload.specification.body,
        criteria=[
            (c.name, c.description, c.weight) for c in payload.evaluation_criteria
        ],
    )
    return serialize_challenge(item)

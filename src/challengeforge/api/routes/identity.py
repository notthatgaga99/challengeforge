from fastapi import APIRouter, Depends

from challengeforge.api.deps import get_current_user, get_uow
from challengeforge.api.schemas import UserResponse
from challengeforge.application import UnitOfWork
from challengeforge.identity import CurrentUser

router = APIRouter()


@router.get("/me", response_model=UserResponse)
async def me(actor: CurrentUser = Depends(get_current_user)) -> UserResponse:
    return UserResponse(id=actor.id, display_name=actor.display_name, role=actor.role.value)


@router.get("/dev/users", response_model=list[UserResponse])
async def list_dev_users(uow: UnitOfWork = Depends(get_uow)) -> list[UserResponse]:
    """Development identity directory. Replace when real authentication exists."""
    users = await uow.users.list_all()
    return [
        UserResponse(id=u.id, display_name=u.display_name, role=u.role.value) for u in users
    ]

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from challengeforge.api.deps import get_db_session

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(session: AsyncSession = Depends(get_db_session)) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ready"}

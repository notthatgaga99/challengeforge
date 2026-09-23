from fastapi import APIRouter, Depends, Request
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


@router.get("/debug/interactive-metrics")
async def interactive_metrics(request: Request) -> dict:
    """Process-local interactive windows + publisher counters (experiment/debug)."""
    publisher = getattr(request.app.state, "interactive_metrics", None)
    if publisher is None:
        return {"available": False}
    snap = publisher.snapshot()
    snap["available"] = True
    return snap

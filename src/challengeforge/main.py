from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from challengeforge.api.errors import register_error_handlers
from challengeforge.api.middleware import RequestContextMiddleware
from challengeforge.api.routes import (
    challenge_router,
    hackathon_router,
    health_router,
    identity_router,
)
from challengeforge.config import Settings, get_settings
from challengeforge.observability import configure_logging
from challengeforge.persistence.seed import seed_dev_users
from challengeforge.persistence.session import dispose_engine, get_session_factory, init_engine
from challengeforge.storage.filesystem import LocalFilesystemStorage
from challengeforge.web.routes import router as web_router

WEB_DIR = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    init_engine(settings)
    LocalFilesystemStorage(settings.artifact_root)
    factory = get_session_factory()
    async with factory() as session:
        await seed_dev_users(session)
    from challengeforge.api.interactive_metrics import get_or_create_publisher

    publisher = get_or_create_publisher(app, settings)
    publisher.start_background()
    try:
        yield
    finally:
        await publisher.stop_background()
        await dispose_engine()

def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    app = FastAPI(
        title="ChallengeForge",
        version="0.1.0",
        description="Modular monolith for hackathon challenge management.",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)

    app.include_router(health_router)
    app.include_router(identity_router, prefix="/api/v1")
    app.include_router(hackathon_router, prefix="/api/v1")
    app.include_router(challenge_router, prefix="/api/v1")
    app.include_router(web_router)

    static_dir = WEB_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


app = create_app()

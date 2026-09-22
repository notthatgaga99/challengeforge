"""Run ChallengeForge locally.

Prefers Postgres on localhost:5432 (docker compose). If that port is closed,
starts the same embedded Postgres the tests use so a locked Docker Hub login
cannot block a laptop demo.
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import uvicorn
from sqlalchemy.ext.asyncio import create_async_engine


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _database_url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    if _port_open(5432):
        return "postgresql+asyncpg://challengeforge:challengeforge@localhost:5432/challengeforge"
    from pgserver import get_server

    data_dir = Path(__file__).resolve().parent.parent / ".pgserver-data"
    data_dir.mkdir(exist_ok=True)
    server = get_server(str(data_dir))
    globals()["_embedded"] = server
    uri = server.get_uri()
    if uri.startswith("postgresql://"):
        uri = "postgresql+asyncpg://" + uri.removeprefix("postgresql://")
    return uri


async def _prepare_schema(url: str) -> None:
    from challengeforge.persistence.models import Base

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


def main() -> None:
    url = _database_url()
    os.environ["DATABASE_URL"] = url
    asyncio.run(_prepare_schema(url))
    from challengeforge.config import get_settings
    from challengeforge.main import create_app

    get_settings.cache_clear()
    app = create_app(get_settings())
    print(f"database={url}")
    print("UI: http://127.0.0.1:8000   API: http://127.0.0.1:8000/docs")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()

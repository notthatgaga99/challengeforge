from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from challengeforge.config import Settings
from challengeforge.identity import ORGANIZER_ID, PARTICIPANT_2_ID, PARTICIPANT_ID
from challengeforge.main import create_app
from challengeforge.persistence.models import Base
from challengeforge.persistence.seed import seed_dev_users
from challengeforge.persistence.session import dispose_engine, get_session_factory, init_engine

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://challengeforge:challengeforge@localhost:5432/challengeforge_test",
)
ADMIN_DATABASE_URL = os.environ.get(
    "ADMIN_DATABASE_URL",
    "postgresql+asyncpg://challengeforge:challengeforge@localhost:5432/challengeforge",
)

_embedded_server = None


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _to_asyncpg(uri: str) -> str:
    if uri.startswith("postgresql+asyncpg://"):
        return uri
    if uri.startswith("postgresql://"):
        return "postgresql+asyncpg://" + uri.removeprefix("postgresql://")
    return uri


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    global _embedded_server
    if os.environ.get("TEST_DATABASE_URL"):
        yield TEST_DATABASE_URL
        return
    if _port_open("127.0.0.1", 5432):
        yield TEST_DATABASE_URL
        return

    # Docker Hub pulls are not always possible (org-locked Docker Desktop).
    # An embedded Postgres keeps tests on a real engine without extra infra.
    from pgserver import get_server

    data_dir = Path(__file__).resolve().parent.parent / ".pgserver-data"
    data_dir.mkdir(exist_ok=True)
    _embedded_server = get_server(str(data_dir))
    yield _to_asyncpg(_embedded_server.get_uri())


async def _ensure_test_database(url: str) -> None:
    if "challengeforge_test" not in url:
        return
    engine = create_async_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = 'challengeforge_test'")
            )
            if result.scalar() is None:
                await conn.execute(text("CREATE DATABASE challengeforge_test"))
    except Exception as exc:
        pytest.skip(f"PostgreSQL is not reachable. Start it with docker compose up -d. ({exc})")
    finally:
        await engine.dispose()


@pytest.fixture
async def app(tmp_path: Path, database_url: str):
    await _ensure_test_database(database_url)
    settings = Settings(
        database_url=database_url,
        artifact_root=tmp_path / "artifacts",
        log_level="WARNING",
    )
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        pytest.skip(f"Could not prepare the test database. ({exc})")
    finally:
        await engine.dispose()

    application = create_app(settings)
    init_engine(settings)
    factory = get_session_factory()
    async with factory() as session:
        await seed_dev_users(session)
    yield application
    await dispose_engine()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    try:
        transport = ASGITransport(app=app, lifespan="off")
    except TypeError:
        transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def organizer_headers() -> dict[str, str]:
    return {"X-User-Id": str(ORGANIZER_ID)}


def participant_headers() -> dict[str, str]:
    return {"X-User-Id": str(PARTICIPANT_ID)}


def participant2_headers() -> dict[str, str]:
    return {"X-User-Id": str(PARTICIPANT_2_ID)}


async def published_challenge(client: AsyncClient) -> dict:
    hackathon = (
        await client.post(
            "/api/v1/hackathons",
            headers=organizer_headers(),
            json={"title": "Forge Cup", "description": "Build something real."},
        )
    ).json()
    await client.post(
        f"/api/v1/hackathons/{hackathon['id']}/publish",
        headers=organizer_headers(),
    )
    challenge = (
        await client.post(
            f"/api/v1/hackathons/{hackathon['id']}/challenges",
            headers=organizer_headers(),
            json={
                "title": "URL shortener",
                "description": "Design a URL shortener.",
                "constraints": "No cloud vendor lock-in in the write-up.",
                "specification": {"body": "Accept a URL and return a short code."},
                "evaluation_criteria": [
                    {
                        "name": "Correctness",
                        "description": "Covers the required behavior.",
                        "weight": 3,
                    }
                ],
            },
        )
    ).json()
    await client.post(
        f"/api/v1/challenges/{challenge['id']}/publish",
        headers=organizer_headers(),
    )
    return challenge

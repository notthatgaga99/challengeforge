"""Dense / hybrid semantic retrieval tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from challengeforge.domain.enums import (
    ChallengeStatus,
    HackathonStatus,
    SubmissionStatus,
)
from challengeforge.identity import ORGANIZER_ID, PARTICIPANT_ID
from challengeforge.ingestion.canonical import PARSER_VERSION, normalize_to_canonical
from challengeforge.ingestion.chunking import CHUNKER_VERSION, chunk_hybrid
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.models import (
    Base,
    ChallengeRow,
    HackathonRow,
    IngestionJobRow,
    SubmissionRow,
)
from challengeforge.persistence.repositories import DocumentChunkRepository
from challengeforge.persistence.seed import seed_dev_users
from challengeforge.retrieval.dense import (
    RETRIEVAL_VERSION_DENSE,
    RETRIEVAL_VERSION_HYBRID_RRF,
    index_chunks,
    rrf_fuse,
)
from challengeforge.retrieval.embeddings import (
    CurriculumConceptEmbeddingProvider,
    StubHashEmbeddingProvider,
    cosine_similarity,
)
from challengeforge.retrieval.lexical import RETRIEVAL_VERSION_FTS, retrieve
from challengeforge.retrieval.schema import ensure_lexical_search_schema


def _url_from_postmaster(data) -> str | None:
    from pathlib import Path

    data = Path(data)
    pid_path = data / "postmaster.pid"
    if not pid_path.is_file():
        return None
    lines = pid_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    # pgserver postmaster.pid: pid, data, ..., port on line index 3
    if len(lines) < 4:
        return None
    port = lines[3].strip()
    if not port.isdigit():
        return None
    return f"postgresql+asyncpg://postgres:@127.0.0.1:{port}/postgres"


async def _prepare(_database_url: str = ""):
    """Use experiment embedded Postgres (read port from postmaster.pid)."""
    import os
    from pathlib import Path

    from pgserver import get_server

    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
        "EXPERIMENT_DATABASE_URL"
    )
    if not url:
        data = Path(__file__).resolve().parents[1] / ".pgserver-sem-exp"
        data.mkdir(exist_ok=True)
        url = _url_from_postmaster(data)
        if not url:
            try:
                srv = get_server(str(data))
                url = srv.get_uri()
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"embedded Postgres unavailable: {exc}")
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await ensure_lexical_search_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _seed_docs(factory, texts: list[str]):
    async with factory() as session:
        await seed_dev_users(session)
        now = utcnow()
        hack = HackathonRow(
            id=uuid4(),
            title="H",
            description="d",
            status=HackathonStatus.PUBLISHED.value,
            organizer_id=ORGANIZER_ID,
            created_at=now,
            updated_at=now,
        )
        session.add(hack)
        chal = ChallengeRow(
            id=uuid4(),
            hackathon_id=hack.id,
            title="C",
            description="d",
            constraints="",
            status=ChallengeStatus.PUBLISHED.value,
            created_at=now,
            updated_at=now,
        )
        session.add(chal)
        await session.flush()
        all_chunks = []
        for i, text in enumerate(texts):
            sub = SubmissionRow(
                id=uuid4(),
                challenge_id=chal.id,
                participant_id=PARTICIPANT_ID,
                status=SubmissionStatus.CREATED.value,
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
            session.add(sub)
            await session.flush()
            job_id = uuid4()
            session.add(
                IngestionJobRow(
                    id=job_id,
                    submission_id=sub.id,
                    artifact_key=f"d/{i}.md",
                    status="succeeded",
                    attempt_count=1,
                    worker_id="t",
                    available_at=now,
                    started_at=now,
                    completed_at=now,
                    error_code=None,
                    error_message=None,
                    result_key=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            drafts = chunk_hybrid(normalize_to_canonical(text), max_chars=1200)
            chunks = await DocumentChunkRepository(session).replace_for_job(
                ingestion_job_id=job_id,
                submission_id=sub.id,
                artifact_key=f"d/{i}.md",
                parser_version=PARSER_VERSION,
                chunker_version=CHUNKER_VERSION,
                chunks=drafts,
            )
            all_chunks.extend(chunks)
        await session.commit()
        return all_chunks


def test_stub_deterministic_and_normalized():
    p = StubHashEmbeddingProvider(dimensions=32)
    a = p.embed_query("hello world")
    b = p.embed_query("hello world")
    assert a == b
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6


def test_curriculum_places_paraphrase_nearer_than_unrelated():
    p = CurriculumConceptEmbeddingProvider(dimensions=64)
    recovery = p.embed_documents(
        ["stale RUNNING evaluations are requeued after worker failure"]
    )[0]
    paraphrase = p.embed_query(
        "how does the system recover work after a worker crashes?"
    )
    unrelated = p.embed_documents(["upload streaming finalize multipart bodies"])[0]
    assert cosine_similarity(paraphrase, recovery) > cosine_similarity(
        paraphrase, unrelated
    )


def test_minilm_places_paraphrase_nearer_than_unrelated():
    """Optional: torch+MiniLM under pytest often hangs on this Windows/OneDrive setup.

    Measured proof lives in ``docs/minilm-bakeoff-results.json``
    (``scripts/minilm_bakeoff_experiment.py``). Set ``CF_RUN_MINILM=1`` to force.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    if os.environ.get("CF_RUN_MINILM") != "1":
        pytest.skip("set CF_RUN_MINILM=1 to load MiniLM under pytest (see bakeoff script)")

    script = r"""
from challengeforge.retrieval.embeddings import LocalMiniLMEmbeddingProvider, cosine_similarity
p = LocalMiniLMEmbeddingProvider()
assert p.dimensions == 384
assert p.embedding_version.startswith("local:all-MiniLM-L6-v2:")
recovery = p.embed_documents(
    ["stale RUNNING evaluations are requeued after worker failure"]
)[0]
paraphrase = p.embed_query(
    "how does the system recover work after a worker crashes?"
)
unrelated = p.embed_documents(["upload streaming finalize multipart bodies"])[0]
assert cosine_similarity(paraphrase, recovery) > cosine_similarity(paraphrase, unrelated)
print("minilm_ok")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={
            **os.environ,
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_DISABLE_PROGRESS_BARS": "1",
            "PYTHONPATH": "src",
        },
    )
    if proc.returncode != 0:
        pytest.fail(f"MiniLM subprocess failed: {proc.stderr[-800:]}")
    assert "minilm_ok" in proc.stdout


def test_rrf_fuse_deterministic():
    a, b, c = uuid4(), uuid4(), uuid4()
    fused = rrf_fuse([[a, b], [b, c]])
    assert fused[0][0] == b  # appears in both lists
    again = rrf_fuse([[a, b], [b, c]])
    assert fused == again


@pytest.mark.asyncio
async def test_dense_and_hybrid_retrieve():
    engine, factory = await _prepare()
    provider = CurriculumConceptEmbeddingProvider(dimensions=64)
    await _seed_docs(
        factory,
        [
            "# Worker crash recovery\n\n"
            "stale RUNNING evaluations are requeued after worker failure.\n",
            "# Uploads\n\nStream multipart to .incoming then atomic finalize.\n",
            "# Claiming\n\nWorkers claim using FOR UPDATE SKIP LOCKED.\n",
        ],
    )
    async with factory() as session:
        chunks = await DocumentChunkRepository(session).list_succeeded()
        await index_chunks(session, provider, chunks)
        await session.commit()

    async with factory() as session:
        dense = await retrieve(
            session,
            "how does the system recover work after a worker crashes?",
            top_k=3,
            retrieval_version=RETRIEVAL_VERSION_DENSE,
            embedding_provider=provider,
        )
        hybrid = await retrieve(
            session,
            "how does the system recover work after a worker crashes?",
            top_k=3,
            retrieval_version=RETRIEVAL_VERSION_HYBRID_RRF,
            embedding_provider=provider,
        )
        lex = await retrieve(
            session,
            "FOR UPDATE SKIP LOCKED",
            top_k=3,
            retrieval_version=RETRIEVAL_VERSION_FTS,
        )
    assert dense
    assert "requeued" in dense[0].content or "RUNNING" in dense[0].content
    assert hybrid
    assert hybrid[0].retrieval_version == RETRIEVAL_VERSION_HYBRID_RRF
    assert lex and "SKIP LOCKED" in lex[0].content
    await engine.dispose()


@pytest.mark.asyncio
async def test_dense_requires_provider():
    engine, factory = await _prepare()
    async with factory() as session:
        with pytest.raises(ValueError, match="embedding_provider"):
            await retrieve(
                session,
                "worker crash",
                retrieval_version=RETRIEVAL_VERSION_DENSE,
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_running_job_embeddings_not_searched():
    engine, factory = await _prepare()
    provider = StubHashEmbeddingProvider(dimensions=32)
    async with factory() as session:
        await seed_dev_users(session)
        now = utcnow()
        hack = HackathonRow(
            id=uuid4(),
            title="H",
            description="d",
            status=HackathonStatus.PUBLISHED.value,
            organizer_id=ORGANIZER_ID,
            created_at=now,
            updated_at=now,
        )
        session.add(hack)
        chal = ChallengeRow(
            id=uuid4(),
            hackathon_id=hack.id,
            title="C",
            description="d",
            constraints="",
            status=ChallengeStatus.PUBLISHED.value,
            created_at=now,
            updated_at=now,
        )
        session.add(chal)
        await session.flush()
        sub = SubmissionRow(
            id=uuid4(),
            challenge_id=chal.id,
            participant_id=PARTICIPANT_ID,
            status=SubmissionStatus.CREATED.value,
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
        session.add(sub)
        await session.flush()
        job_id = uuid4()
        session.add(
            IngestionJobRow(
                id=job_id,
                submission_id=sub.id,
                artifact_key="x.md",
                status="running",
                attempt_count=1,
                worker_id="t",
                available_at=now,
                started_at=now,
                completed_at=None,
                error_code=None,
                error_message=None,
                result_key=None,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        drafts = chunk_hybrid(
            normalize_to_canonical("UniqueTokenSemanticXYZ should hide"),
            max_chars=200,
        )
        chunks = await DocumentChunkRepository(session).replace_for_job(
            ingestion_job_id=job_id,
            submission_id=sub.id,
            artifact_key="x.md",
            parser_version=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
            chunks=drafts,
        )
        await index_chunks(session, provider, chunks)
        await session.commit()

    async with factory() as session:
        hits = await retrieve(
            session,
            "UniqueTokenSemanticXYZ",
            top_k=5,
            retrieval_version=RETRIEVAL_VERSION_DENSE,
            embedding_provider=provider,
        )
    assert hits == []
    await engine.dispose()

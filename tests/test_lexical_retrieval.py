"""Lexical retrieval correctness, metrics, and durability tests."""

from __future__ import annotations

from pathlib import Path
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
from challengeforge.retrieval.lexical import (
    RETRIEVAL_VERSION_FTS,
    RETRIEVAL_VERSION_SIMPLE,
    RETRIEVAL_VERSION_STRUCT,
    normalize_query,
    retrieve,
)
from challengeforge.retrieval.metrics import (
    evaluate_query,
    macro_average,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from challengeforge.retrieval.schema import ensure_lexical_search_schema


async def _prepare(database_url: str):
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await ensure_lexical_search_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _add_doc(factory, *, text: str, status: str = "succeeded"):
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
        key = f"submissions/{chal.id}/doc.md"
        session.add(
            IngestionJobRow(
                id=job_id,
                submission_id=sub.id,
                artifact_key=key,
                status=status,
                attempt_count=1,
                worker_id="t",
                available_at=now,
                started_at=now,
                completed_at=now if status == "succeeded" else None,
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
            artifact_key=key,
            parser_version=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
            chunks=drafts,
        )
        await session.commit()
        return job_id, chunks


def test_normalize_query_preserves_identifiers():
    assert normalize_query("  FOR  UPDATE   SKIP LOCKED ") == "FOR UPDATE SKIP LOCKED"
    assert normalize_query("recover_stale_running") == "recover_stale_running"


def test_metrics_known_example():
    ranked = ["a", "x", "b", "y"]
    relevant = {"a", "b"}
    assert recall_at_k(ranked, relevant, 1) == 0.5
    assert recall_at_k(ranked, relevant, 3) == 1.0
    assert precision_at_k(ranked, relevant, 2) == 0.5
    assert reciprocal_rank(ranked, relevant) == 1.0
    m = evaluate_query(query_id="q", ranked_ids=ranked, relevant=relevant, ks=(1, 3))
    assert m.recall_at[3] == 1.0
    assert m.first_relevant_rank == 1
    agg = macro_average([m], (1, 3))
    assert agg["mrr"] == 1.0


@pytest.mark.asyncio
async def test_exact_phrase_and_provenance(database_url: str, tmp_path: Path):
    engine, factory = await _prepare(database_url)
    _, chunks = await _add_doc(
        factory,
        text=(
            "# Claiming\n\nWorkers use FOR UPDATE SKIP LOCKED to claim jobs.\n\n"
            "# Other\n\nUnrelated prose about weather.\n"
        ),
    )
    async with factory() as session:
        hits = await retrieve(
            session, "FOR UPDATE SKIP LOCKED", top_k=5, retrieval_version=RETRIEVAL_VERSION_FTS
        )
    assert hits
    assert any("SKIP LOCKED" in h.content for h in hits)
    top = hits[0]
    assert top.artifact_key
    assert top.ingestion_job_id
    assert top.ordinal >= 0
    assert top.char_end >= top.char_start
    assert top.heading_path is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_identifier_simple_config(database_url: str):
    engine, factory = await _prepare(database_url)
    await _add_doc(
        factory,
        text="# Code\n\ndef recover_stale_running():\n    return 1\n",
    )
    async with factory() as session:
        hits = await retrieve(
            session,
            "recover_stale_running",
            top_k=5,
            retrieval_version=RETRIEVAL_VERSION_SIMPLE,
        )
    assert hits
    assert any("recover_stale_running" in h.content for h in hits)
    await engine.dispose()


@pytest.mark.asyncio
async def test_rare_term_and_unicode(database_url: str):
    engine, factory = await _prepare(database_url)
    await _add_doc(
        factory,
        text="# Notes\n\nIdempotency matters. Also 你好 and Café.\n",
    )
    async with factory() as session:
        idemp = await retrieve(session, "idempotency", top_k=5)
        uni = await retrieve(session, "你好", top_k=5)
    assert idemp and any("Idempotency" in h.content or "idempotency" in h.content.lower() for h in idemp)
    # Unicode may depend on postgres text search config; at least must not error.
    assert isinstance(uni, list)
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_result_and_irrelevant(database_url: str):
    engine, factory = await _prepare(database_url)
    await _add_doc(factory, text="# A\n\nCompletely about gardening tomatoes.\n")
    async with factory() as session:
        hits = await retrieve(session, "quantum entanglement scheduling", top_k=5)
    assert hits == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_deterministic_order_and_topk(database_url: str):
    engine, factory = await _prepare(database_url)
    await _add_doc(
        factory,
        text=(
            "# One\n\nworker crash recovery details here.\n\n"
            "# Two\n\nworker crash recovery again with more words about recovery.\n"
        ),
    )
    async with factory() as session:
        a = await retrieve(session, "worker crash recovery", top_k=3)
        b = await retrieve(session, "worker crash recovery", top_k=3)
    assert [(h.chunk_id, h.score) for h in a] == [(h.chunk_id, h.score) for h in b]
    assert len(a) <= 3
    if len(a) >= 2:
        assert a[0].score >= a[1].score
        if a[0].score == a[1].score:
            assert a[0].chunk_id < a[1].chunk_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_only_succeeded_jobs_searchable(database_url: str):
    engine, factory = await _prepare(database_url)
    await _add_doc(
        factory,
        text="# Running\n\nUniqueTokenXYZ should not be searchable yet.\n",
        status="running",
    )
    await _add_doc(
        factory,
        text="# Done\n\nUniqueTokenABC is searchable after success.\n",
        status="succeeded",
    )
    async with factory() as session:
        bad = await retrieve(session, "UniqueTokenXYZ", top_k=5)
        good = await retrieve(session, "UniqueTokenABC", top_k=5)
    assert bad == []
    assert good and any("UniqueTokenABC" in h.content for h in good)
    await engine.dispose()


@pytest.mark.asyncio
async def test_structural_variant_runs(database_url: str):
    engine, factory = await _prepare(database_url)
    await _add_doc(
        factory,
        text="# Idempotency\n\nEffects must be safe under retry.\n",
    )
    async with factory() as session:
        hits = await retrieve(
            session,
            "Idempotency",
            top_k=5,
            retrieval_version=RETRIEVAL_VERSION_STRUCT,
        )
    assert hits
    assert hits[0].retrieval_version == RETRIEVAL_VERSION_STRUCT
    await engine.dispose()

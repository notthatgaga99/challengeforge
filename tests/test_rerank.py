"""Cross-encoder-style rerank stage (cf-hybrid-rerank-1)."""

from __future__ import annotations

from uuid import uuid4

import pytest

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
    ChallengeRow,
    HackathonRow,
    IngestionJobRow,
    SubmissionRow,
)
from challengeforge.persistence.repositories import DocumentChunkRepository
from challengeforge.persistence.seed import seed_dev_users
from challengeforge.retrieval.dense import (
    RETRIEVAL_VERSION_HYBRID_RRF,
    index_chunks,
)
from challengeforge.retrieval.embeddings import (
    CurriculumConceptEmbeddingProvider,
    version_requires_embeddings,
)
from challengeforge.retrieval.lexical import RETRIEVAL_VERSION_FTS, RetrievedChunk, retrieve
from challengeforge.retrieval.rerank import (
    RETRIEVAL_VERSION_HYBRID_RERANK,
    LexicalOverlapRerankProvider,
    rerank_hits,
)
from tests.test_semantic_retrieval import _prepare


def test_rerank_version_requires_embeddings():
    assert version_requires_embeddings(RETRIEVAL_VERSION_HYBRID_RERANK)
    assert not version_requires_embeddings(RETRIEVAL_VERSION_FTS)


def test_lexical_overlap_rerank_orders_by_query_coverage():
    reranker = LexicalOverlapRerankProvider()
    a = uuid4()
    b = uuid4()
    hits = [
        RetrievedChunk(
            chunk_id=a,
            ingestion_job_id=uuid4(),
            submission_id=uuid4(),
            artifact_key="a",
            ordinal=0,
            content="completely unrelated gardening tips",
            score=0.9,
            char_start=0,
            char_end=10,
            line_start=1,
            line_end=1,
            block_type="paragraph",
            heading_path=(),
            parser_version="p",
            chunker_version="c",
            retrieval_version=RETRIEVAL_VERSION_HYBRID_RRF,
            content_sha256="x",
        ),
        RetrievedChunk(
            chunk_id=b,
            ingestion_job_id=uuid4(),
            submission_id=uuid4(),
            artifact_key="b",
            ordinal=1,
            content="worker crash recovery requeues stale running jobs",
            score=0.1,
            char_start=0,
            char_end=10,
            line_start=1,
            line_end=1,
            block_type="paragraph",
            heading_path=(),
            parser_version="p",
            chunker_version="c",
            retrieval_version=RETRIEVAL_VERSION_HYBRID_RRF,
            content_sha256="y",
        ),
    ]
    out = rerank_hits(
        "worker crash recovery",
        hits,
        reranker,
        top_k=2,
    )
    assert out[0].chunk_id == b
    assert out[0].retrieval_version == RETRIEVAL_VERSION_HYBRID_RERANK
    assert out[0].score >= out[1].score


def test_rerank_hits_deterministic_tie_break():
    reranker = LexicalOverlapRerankProvider()
    low = uuid4()
    high = uuid4()
    # Force stable ids so tie-break by chunk_id is assertable when scores equal.
    ids = sorted([low, high], key=str)
    hits = []
    for i, cid in enumerate(ids):
        hits.append(
            RetrievedChunk(
                chunk_id=cid,
                ingestion_job_id=uuid4(),
                submission_id=uuid4(),
                artifact_key=str(i),
                ordinal=i,
                content="alpha beta",
                score=float(i),
                char_start=0,
                char_end=1,
                line_start=1,
                line_end=1,
                block_type="paragraph",
                heading_path=(),
                parser_version="p",
                chunker_version="c",
                retrieval_version=RETRIEVAL_VERSION_HYBRID_RRF,
                content_sha256=str(i),
            )
        )
    out = rerank_hits("alpha beta", hits, reranker, top_k=2)
    assert [h.chunk_id for h in out] == ids


@pytest.mark.asyncio
async def test_retrieve_hybrid_rerank_end_to_end():
    engine, factory = await _prepare()
    try:
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
            texts = [
                "# Recovery\n\nstale RUNNING evaluations are requeued after worker failure.\n",
                "# Uploads\n\nStream multipart to .incoming then atomic finalize.\n",
            ]
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
                await index_chunks(
                    session, CurriculumConceptEmbeddingProvider(), chunks
                )
            await session.commit()

        provider = CurriculumConceptEmbeddingProvider()
        reranker = LexicalOverlapRerankProvider()
        async with factory() as session:
            hits = await retrieve(
                session,
                "how does the system recover work after a worker crashes?",
                top_k=5,
                retrieval_version=RETRIEVAL_VERSION_HYBRID_RERANK,
                embedding_provider=provider,
                rerank_provider=reranker,
            )
            assert len(hits) >= 1
            assert hits[0].retrieval_version == RETRIEVAL_VERSION_HYBRID_RERANK
            assert "requeue" in hits[0].content.lower() or "running" in hits[0].content.lower()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rerank_requires_embedding_provider():
    engine, factory = await _prepare()
    try:
        async with factory() as session:
            with pytest.raises(ValueError, match="embedding_provider"):
                await retrieve(
                    session,
                    "query",
                    retrieval_version=RETRIEVAL_VERSION_HYBRID_RERANK,
                )
    finally:
        await engine.dispose()

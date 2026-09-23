"""Chunking & representation correctness, provenance, and durability tests."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from challengeforge.config import Settings
from challengeforge.domain.enums import (
    ChallengeStatus,
    HackathonStatus,
    IngestionStatus,
    SubmissionStatus,
)
from challengeforge.identity import ORGANIZER_ID, PARTICIPANT_ID
from challengeforge.ingestion.canonical import PARSER_VERSION, normalize_to_canonical
from challengeforge.ingestion.chunking import (
    CHUNKER_VERSION,
    chunk_fixed_chars,
    chunk_hybrid,
)
from challengeforge.ingestion.processor import process_artifact
from challengeforge.ingestion.worker import IngestionWorker
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.models import (
    Base,
    ChallengeRow,
    HackathonRow,
    SubmissionRow,
)
from challengeforge.persistence.repositories import (
    DocumentChunkRepository,
    IngestionJobRepository,
)
from challengeforge.persistence.seed import seed_dev_users
from challengeforge.storage.filesystem import LocalFilesystemStorage


MARKDOWN = """# Title

Intro paragraph.

## Section A

Body under A with enough text to matter.

```python
def hello():
    return "世界"
```

## Section B

- one
- two

Closing prose.
"""


def test_normalize_preserves_structure_and_unicode():
    doc = normalize_to_canonical("A  \r\n\r\nB\t\n你好\n")
    assert "\r" not in doc.text
    assert doc.text == "A\n\nB\n你好"
    assert "你好" in doc.text
    assert doc.parser_version == PARSER_VERSION


def test_hybrid_deterministic_ordered_no_empty():
    doc = normalize_to_canonical(MARKDOWN)
    a = chunk_hybrid(doc, max_chars=100)
    b = chunk_hybrid(doc, max_chars=100)
    assert [(c.ordinal, c.content, c.content_sha256) for c in a] == [
        (c.ordinal, c.content, c.content_sha256) for c in b
    ]
    assert [c.ordinal for c in a] == list(range(len(a)))
    assert all(c.content.strip() for c in a)
    assert all(len(c.content) <= 100 for c in a)


def test_provenance_exact_span():
    doc = normalize_to_canonical(MARKDOWN)
    chunks = chunk_hybrid(doc, max_chars=120)
    covered = 0
    prev_end = 0
    for c in chunks:
        assert doc.text[c.char_start : c.char_end] == c.content
        assert 1 <= c.line_start <= c.line_end <= doc.line_count
        assert c.char_start >= prev_end  # no overlap policy
        prev_end = c.char_end
        covered += c.char_end - c.char_start
    # Gaps (blank lines between unpacked units) are allowed; no overlap.
    assert covered <= len(doc.text)


def test_markdown_heading_path():
    doc = normalize_to_canonical(MARKDOWN)
    # Small budget keeps sections in separate chunks so paths are observable.
    chunks = chunk_hybrid(doc, max_chars=80)
    assert any(c.heading_path and c.heading_path[0] == "Title" for c in chunks)
    assert any("Section A" in c.heading_path for c in chunks)
    assert any("Section B" in c.heading_path for c in chunks)


def test_code_fence_not_split_when_small():
    doc = normalize_to_canonical(MARKDOWN)
    chunks = chunk_hybrid(doc, max_chars=500)
    fenced = [c for c in chunks if "```python" in c.content]
    assert fenced
    assert all("def hello" in c.content for c in fenced)


def test_oversized_logical_unit_hard_split():
    doc = normalize_to_canonical("x" * 5000)
    chunks = chunk_hybrid(doc, max_chars=200)
    assert len(chunks) >= 25
    assert all(len(c.content) <= 200 for c in chunks)
    assert any(c.oversized_split for c in chunks)


def test_pathological_long_line():
    doc = normalize_to_canonical("y" * 3000)
    chunks = chunk_hybrid(doc, max_chars=128)
    assert all(len(c.content) <= 128 for c in chunks)
    assert all(doc.text[c.char_start : c.char_end] == c.content for c in chunks)


def test_empty_yields_no_chunks():
    assert chunk_hybrid(normalize_to_canonical("")) == []
    assert chunk_hybrid(normalize_to_canonical("\n\n  \n")) == []


def test_unicode_budget_is_codepoints_not_bytes():
    # Each emoji is one Python char but multiple UTF-8 bytes.
    doc = normalize_to_canonical("🚀" * 100)
    chunks = chunk_hybrid(doc, max_chars=40)
    assert all(len(c.content) <= 40 for c in chunks)
    assert sum(len(c.content) for c in chunks) == 100


def test_fixed_vs_hybrid_differ_on_markdown():
    doc = normalize_to_canonical(MARKDOWN * 3)
    fixed = chunk_fixed_chars(doc, max_chars=80)
    hybrid = chunk_hybrid(doc, max_chars=80)
    # Hybrid should preserve more heading metadata
    assert any(c.heading_path for c in hybrid)
    assert all(c.heading_path == () for c in fixed)


def test_chunker_version_constant():
    assert CHUNKER_VERSION.startswith("cf-chunk-")


@pytest.mark.asyncio
async def test_ingestion_persists_chunks_idempotently(database_url: str, tmp_path: Path):
    engine = create_async_engine(database_url)
    settings = Settings(artifact_root=tmp_path / "art", ingestion_chunk_max_chars=200)
    storage = LocalFilesystemStorage(settings.artifact_root)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

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
        key = f"submissions/{chal.id}/{PARTICIPANT_ID}/doc.md"
        storage.put(key, MARKDOWN.encode("utf-8"), "text/markdown")
        job = await IngestionJobRepository(session).enqueue(
            submission_id=sub.id, artifact_key=key
        )
        await session.commit()
        job_id = job.id

    worker = IngestionWorker(
        worker_id="chunk-w",
        session_factory=factory,
        storage=storage,
        settings=settings,
    )
    assert await worker.run_once()
    assert worker.succeeded == 1

    async with factory() as session:
        job = await IngestionJobRepository(session).get(job_id)
        assert job.status == IngestionStatus.SUCCEEDED
        chunks = await DocumentChunkRepository(session).list_for_job(job_id)
        assert len(chunks) >= 1
        assert chunks[0].parser_version == PARSER_VERSION
        assert chunks[0].chunker_version == CHUNKER_VERSION
        manifest = json.loads(storage.get(job.result_key).decode("utf-8"))
        assert manifest["stage"] == "CHUNKED"
        assert manifest["chunk_count"] == len(chunks)
        assert storage.exists(manifest["canonical_key"])

    # Idempotent re-process via second job on same bytes yields same content hashes
    result = process_artifact(
        storage, job_id=job_id, artifact_key=key, attempt_count=1, max_chunk_chars=200
    )
    assert [c.content_sha256 for c in result.chunks] == [c.content_sha256 for c in chunks]
    await engine.dispose()


@pytest.mark.asyncio
async def test_partial_chunk_persist_not_succeeded(database_url: str, tmp_path: Path):
    engine = create_async_engine(database_url)
    settings = Settings(artifact_root=tmp_path / "art2", ingestion_chunk_max_chars=200)
    storage = LocalFilesystemStorage(settings.artifact_root)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

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
        key = f"submissions/{chal.id}/{PARTICIPANT_ID}/p.md"
        storage.put(key, b"# Hi\n\nWorld\n", "text/markdown")
        job = await IngestionJobRepository(session).enqueue(
            submission_id=sub.id, artifact_key=key
        )
        await session.commit()
        job_id = job.id

    worker = IngestionWorker(
        worker_id="partial",
        session_factory=factory,
        storage=storage,
        settings=settings,
    )
    worker.crash_after_chunk_persist = True
    with pytest.raises(RuntimeError, match="chunk persist"):
        await worker.run_once()

    async with factory() as session:
        job = await IngestionJobRepository(session).get(job_id)
        assert job.status == IngestionStatus.RUNNING
        # Chunks may exist transiently; consumers must not treat RUNNING as complete.
        assert job.result_key is None

    await engine.dispose()

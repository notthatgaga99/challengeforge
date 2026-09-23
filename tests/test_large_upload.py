"""Tests for streaming upload + temp/finalize boundary."""

from __future__ import annotations

import io
from pathlib import Path
from uuid import uuid4

import pytest

from challengeforge.application.uploads import (
    IncomingUpload,
    cleanup_incoming,
    sync_stream_to_incoming,
)
from challengeforge.domain.exceptions import ValidationFailed
from challengeforge.storage.filesystem import LocalFilesystemStorage


@pytest.fixture
def store(tmp_path: Path) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(tmp_path / "artifacts")


def test_stream_finalize_publishes_complete_blob(store: LocalFilesystemStorage):
    payload = b"hello-stream" * 100
    with io.BytesIO(payload) as fh:
        incoming = sync_stream_to_incoming(
            store.incoming_root(), fh, max_bytes=len(payload)
        )
    key = f"submissions/{uuid4()}.bin"
    store.put_from_path(key, incoming.staging_path, incoming.content_type)
    incoming.mark_finalized()
    assert store.get(key) == payload
    assert not incoming.staging_path.exists()


def test_abort_removes_partial_staging(store: LocalFilesystemStorage):
    path = store.incoming_root() / str(uuid4())
    path.write_bytes(b"partial")
    inc = IncomingUpload(path, 7, "abc", "application/octet-stream")
    inc.abort()
    assert not path.exists()


def test_oversize_leaves_no_staging(store: LocalFilesystemStorage):
    before = set(store.incoming_root().glob("*"))
    with pytest.raises(ValidationFailed):
        with io.BytesIO(b"x" * 100) as fh:
            sync_stream_to_incoming(store.incoming_root(), fh, max_bytes=10)
    after = set(store.incoming_root().glob("*"))
    assert after == before


def test_checksum_mismatch_rejected(store: LocalFilesystemStorage):
    with pytest.raises(ValidationFailed, match="checksum"):
        with io.BytesIO(b"abc") as fh:
            sync_stream_to_incoming(
                store.incoming_root(),
                fh,
                max_bytes=100,
                expected_sha256="0" * 64,
            )


def test_checksum_match_accepted(store: LocalFilesystemStorage):
    import hashlib

    data = b"abc123"
    digest = hashlib.sha256(data).hexdigest()
    with io.BytesIO(data) as fh:
        inc = sync_stream_to_incoming(
            store.incoming_root(),
            fh,
            max_bytes=100,
            expected_sha256=digest,
        )
    assert inc.sha256 == digest


def test_cleanup_incoming_idempotent(store: LocalFilesystemStorage, tmp_path: Path):
    import os
    import time

    stale = store.incoming_root() / str(uuid4())
    stale.write_bytes(b"old")
    os.utime(stale, (time.time() - 9999, time.time() - 9999))
    d1 = cleanup_incoming(store.incoming_root(), grace_seconds=1.0)
    d2 = cleanup_incoming(store.incoming_root(), grace_seconds=1.0)
    assert stale.name in d1
    assert stale.name not in d2
    assert not stale.exists()


def test_final_key_never_partial(store: LocalFilesystemStorage):
    """If finalize hasn't run, final key must not exist."""
    key = f"submissions/{uuid4()}.bin"
    assert not store.exists(key)
    with io.BytesIO(b"data") as fh:
        inc = sync_stream_to_incoming(store.incoming_root(), fh, max_bytes=100)
    assert not store.exists(key)
    store.put_from_path(key, inc.staging_path, "application/octet-stream")
    assert store.exists(key)
    assert store.get(key) == b"data"


@pytest.mark.asyncio
async def test_attach_incoming_compensates_on_commit_fail(tmp_path: Path):
    from unittest.mock import AsyncMock, MagicMock

    from challengeforge.application import UnitOfWork
    from challengeforge.application.submissions import SubmissionService
    from challengeforge.application.uploads import IncomingUpload
    from challengeforge.config import Settings
    from challengeforge.domain.enums import SubmissionStatus, UserRole
    from challengeforge.identity import CurrentUser
    from challengeforge.persistence.models import SubmissionRow

    store = LocalFilesystemStorage(tmp_path / "artifacts")
    settings = Settings(artifact_root=tmp_path / "artifacts", artifact_max_bytes=1024)
    with io.BytesIO(b"payload") as fh:
        incoming = sync_stream_to_incoming(store.incoming_root(), fh, max_bytes=1024)

    participant = uuid4()
    row = MagicMock(spec=SubmissionRow)
    row.id = uuid4()
    row.participant_id = participant
    row.challenge_id = uuid4()
    row.status = SubmissionStatus.CREATED.value
    row.artifact_key = None

    session = AsyncMock()
    session.commit = AsyncMock(side_effect=RuntimeError("db down"))
    session.rollback = AsyncMock()
    submissions_repo = MagicMock()
    submissions_repo.get_row = AsyncMock(return_value=row)
    submissions_repo.get = AsyncMock(return_value=None)

    uow = MagicMock(spec=UnitOfWork)
    uow.session = session
    uow.storage = store
    uow.settings = settings
    uow.submissions = submissions_repo

    service = SubmissionService(uow)
    actor = CurrentUser(id=participant, display_name="P", role=UserRole.PARTICIPANT)
    with pytest.raises(RuntimeError, match="db down"):
        await service.attach_incoming(actor, row.id, incoming, artifact_filename="a.bin")
    assert list(store.iter_keys("submissions/")) == []

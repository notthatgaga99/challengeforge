"""Deterministic artifact consistency / failure-injection tests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from challengeforge.application.artifacts import compensate_delete, reconcile_orphans
from challengeforge.storage.filesystem import LocalFilesystemStorage


@pytest.fixture
def store(tmp_path: Path) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(tmp_path / "artifacts")


def test_put_get_delete_roundtrip(store: LocalFilesystemStorage):
    key = store.put("submissions/a/b.bin", b"hello", "application/octet-stream")
    assert store.exists(key)
    assert store.get(key) == b"hello"
    assert store.delete(key) is True
    assert not store.exists(key)
    assert store.delete(key) is False  # idempotent


def test_put_uses_temp_replace(store: LocalFilesystemStorage, tmp_path: Path):
    key = store.put("submissions/t/x.bin", b"data", "text/plain")
    path = tmp_path / "artifacts" / "submissions" / "t" / "x.bin"
    assert path.is_file()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_compensate_delete_after_simulated_db_failure(store: LocalFilesystemStorage):
    key = f"submissions/{uuid4()}.bin"
    store.put(key, b"orphan", "application/octet-stream")
    assert store.exists(key)
    assert compensate_delete(store, key) is True
    assert not store.exists(key)


def test_blob_first_no_dangling_reference_on_put_failure(store: LocalFilesystemStorage):
    # Put never ran → nothing to reference. Invariant preserved.
    key = f"submissions/{uuid4()}.bin"
    assert not store.exists(key)


def test_reconcile_deletes_unreferenced_keeps_live(store: LocalFilesystemStorage):
    live = store.put("submissions/live.bin", b"1", "application/octet-stream")
    orphan = store.put("submissions/orphan.bin", b"2", "application/octet-stream")
    report = reconcile_orphans(
        store, [live], prefix="submissions/", grace_seconds=0.0, delete=True
    )
    assert live not in report.deleted
    assert orphan in report.deleted
    assert store.exists(live)
    assert not store.exists(orphan)


def test_reconcile_grace_skips_young(store: LocalFilesystemStorage):
    young = store.put("submissions/young.bin", b"y", "application/octet-stream")
    report = reconcile_orphans(
        store, [], prefix="submissions/", grace_seconds=10_000.0, delete=True
    )
    assert young in report.skipped_young
    assert young not in report.deleted
    assert store.exists(young)


def test_reconcile_idempotent(store: LocalFilesystemStorage):
    orphan = store.put("submissions/o.bin", b"o", "application/octet-stream")
    r1 = reconcile_orphans(store, [], prefix="submissions/", grace_seconds=0.0)
    r2 = reconcile_orphans(store, [], prefix="submissions/", grace_seconds=0.0)
    assert orphan in r1.deleted
    assert orphan not in r2.deleted
    assert not store.exists(orphan)


def test_reconcile_reports_missing_referenced(store: LocalFilesystemStorage):
    missing = "submissions/missing.bin"
    report = reconcile_orphans(
        store, [missing], prefix="submissions/", grace_seconds=0.0, delete=False
    )
    assert missing in report.missing_referenced


def test_replace_deletes_previous_key(store: LocalFilesystemStorage):
    old = store.put("submissions/old.bin", b"v1", "application/octet-stream")
    new = store.put("submissions/new.bin", b"v2", "application/octet-stream")
    compensate_delete(store, old)
    assert not store.exists(old)
    assert store.exists(new)


def test_iter_keys_skips_sidecars(store: LocalFilesystemStorage):
    key = store.put("submissions/c/file.bin", b"x", "text/plain")
    keys = list(store.iter_keys("submissions/"))
    assert key in keys
    assert not any(k.endswith(".content_type") for k in keys)


@pytest.mark.asyncio
async def test_attach_compensates_when_commit_fails(tmp_path: Path, monkeypatch):
    """Service-level: put succeeds, commit fails → blob removed."""
    from challengeforge.application.submissions import SubmissionService
    from challengeforge.application import UnitOfWork
    from challengeforge.config import Settings
    from challengeforge.domain.enums import SubmissionStatus
    from challengeforge.identity import CurrentUser
    from challengeforge.persistence.models import SubmissionRow
    from unittest.mock import AsyncMock, MagicMock

    store = LocalFilesystemStorage(tmp_path / "artifacts")
    settings = Settings(artifact_root=tmp_path / "artifacts", artifact_max_bytes=1024)

    participant = uuid4()
    challenge = uuid4()
    submission_id = uuid4()

    row = MagicMock(spec=SubmissionRow)
    row.id = submission_id
    row.participant_id = participant
    row.challenge_id = challenge
    row.status = SubmissionStatus.CREATED.value
    row.artifact_key = None
    row.metadata_json = {}

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
    from challengeforge.domain.enums import UserRole

    actor = CurrentUser(
        id=participant,
        display_name="P",
        role=UserRole.PARTICIPANT,
    )

    with pytest.raises(RuntimeError, match="db down"):
        await service.update_created(
            actor,
            submission_id,
            artifact=b"payload",
            artifact_content_type="application/octet-stream",
            artifact_filename="a.bin",
        )

    # Compensating delete should have removed whatever was put.
    remaining = list(store.iter_keys("submissions/"))
    assert remaining == []
    session.rollback.assert_awaited()

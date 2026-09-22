from pathlib import Path

import pytest

from challengeforge.storage.filesystem import LocalFilesystemStorage


def test_put_and_get_roundtrip(tmp_path: Path):
    store = LocalFilesystemStorage(tmp_path)
    key = store.put("submissions/a/file.txt", b"hello", "text/plain")
    assert store.exists(key)
    assert store.get(key) == b"hello"


def test_rejects_path_escape(tmp_path: Path):
    store = LocalFilesystemStorage(tmp_path)
    with pytest.raises(ValueError):
        store.put("../escape.txt", b"nope", "text/plain")

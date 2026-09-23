"""Reproducible experiment path bootstrap."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def test_ensure_experiment_paths_allows_concurrency_import(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Remove scripts from path if present to simulate clean invocation.
    root = Path(__file__).resolve().parent.parent
    scripts = str(root / "scripts")
    while scripts in sys.path:
        sys.path.remove(scripts)
    sys.modules.pop("cf_experiment_paths", None)
    sys.modules.pop("concurrency_experiment", None)

    sys.path.insert(0, scripts)
    from cf_experiment_paths import ensure_experiment_paths

    ensure_experiment_paths()
    mod = importlib.import_module("concurrency_experiment")
    assert hasattr(mod, "utc_iso")
    assert callable(mod.utc_iso)

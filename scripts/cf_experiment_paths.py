"""Bootstrap import paths for ChallengeForge experiment scripts.

Experiment helpers live under ``scripts/`` and are not an installable package.
Always call ``ensure_experiment_paths()`` before importing ``concurrency_experiment``
(or other script modules) so invocation is reproducible from any cwd.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def repo_root() -> Path:
    return _ROOT


def ensure_experiment_paths() -> Path:
    """Insert ``scripts/`` and ``src/`` on ``sys.path`` (idempotent)."""
    scripts = _ROOT / "scripts"
    src = _ROOT / "src"
    for path in (scripts, src):
        s = str(path)
        if s not in sys.path:
            sys.path.insert(0, s)
    return _ROOT

"""Execution environment contract: allowlist, not denylist scrubbing.

Trusted synthetic workloads may receive a narrow runtime PYTHONPATH so
``workloads.py`` can import nothing from the host accidentally polluted env.
That PYTHONPATH still exposes platform source to the child — acceptable only
because the corpus is trusted. Hostile participant code must NOT receive it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

# OS / interpreter knobs required to start a process on this host.
OS_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "PATHEXT",
        "COMSPEC",
        "LANG",
        "LC_ALL",
    }
)

# ChallengeForge-owned knobs injected by the executor.
CF_INJECTED: frozenset[str] = frozenset(
    {
        "CF_WORKSPACE",
        "CF_EXECUTION_ATTEMPT_ID",
        "CF_FORBIDDEN_PROBE",  # boundary experiment only
        "TMPDIR",
        "TEMP",
        "TMP",
        "PYTHONUNBUFFERED",
        "PYTHONPATH",  # trusted corpus only — see build_execution_env
        "CF_PROCESS_HEAVY_N",
        "CF_DISK_HEAVY_MB",
    }
)

# Names that must never appear in an execution env (defense in depth checks).
FORBIDDEN_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "DATABASE_URL",
        "TEST_DATABASE_URL",
        "ADMIN_DATABASE_URL",
        "EXPERIMENT_DATABASE_URL",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "AZURE_CLIENT_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "SSH_AUTH_SOCK",
        "CF_API_SECRET",
        "SECRET_KEY",
        "POSTGRES_PASSWORD",
    }
)


def build_execution_env(
    work_dir: Path,
    *,
    host_environ: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
    include_platform_pythonpath: bool = True,
    execution_attempt_id: str | None = None,
) -> dict[str, str]:
    """Build an allowlisted child environment.

    ``include_platform_pythonpath`` is for trusted corpus runners only.
    """
    host = host_environ if host_environ is not None else os.environ
    env: dict[str, str] = {}
    for key in OS_ALLOWLIST:
        if key in host and host[key]:
            env[key] = host[key]

    env["CF_WORKSPACE"] = str(work_dir)
    env["TMPDIR"] = str(work_dir)
    env["TEMP"] = str(work_dir)
    env["TMP"] = str(work_dir)
    env["PYTHONUNBUFFERED"] = "1"
    if execution_attempt_id:
        env["CF_EXECUTION_ATTEMPT_ID"] = execution_attempt_id

    if include_platform_pythonpath:
        # parents[2] == .../src (package root for challengeforge.*)
        src = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = src

    if extra:
        for k, v in extra.items():
            if k in FORBIDDEN_SECRET_NAMES:
                continue
            env[k] = v

    # Final hard strip of any forbidden names that slipped through extras.
    for bad in FORBIDDEN_SECRET_NAMES:
        env.pop(bad, None)
    return env


def assert_no_forbidden_secrets(env: Mapping[str, str]) -> list[str]:
    """Return forbidden keys present (empty = pass)."""
    return sorted(k for k in env if k in FORBIDDEN_SECRET_NAMES)


def env_contract_summary(env: Mapping[str, str]) -> dict[str, object]:
    return {
        "key_count": len(env),
        "keys": sorted(env.keys()),
        "has_pythonpath": "PYTHONPATH" in env,
        "forbidden_present": assert_no_forbidden_secrets(env),
        "workspace": env.get("CF_WORKSPACE"),
    }

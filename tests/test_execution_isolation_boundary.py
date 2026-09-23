"""Deterministic isolation-boundary contract tests (no host escape attempts)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from challengeforge.execution.execution_environment import (
    FORBIDDEN_SECRET_NAMES,
    assert_no_forbidden_secrets,
    build_execution_env,
)
from challengeforge.execution.limits import ExecutionLimits
from challengeforge.execution.ownership import contain_attempt, new_attempt, pid_alive
from challengeforge.execution.process_executor import ProcessExecutor


def test_env_allowlist_strips_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "hunter2")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    env = build_execution_env(tmp_path / "ws", include_platform_pythonpath=True)
    assert assert_no_forbidden_secrets(env) == []
    for name in ("DATABASE_URL", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY"):
        assert name not in env
    assert env["CF_WORKSPACE"] == str(tmp_path / "ws")
    assert "PYTHONPATH" in env


def test_env_rejects_secret_injection_via_extra(tmp_path: Path):
    env = build_execution_env(
        tmp_path,
        extra={"DATABASE_URL": "postgresql://injected", "CF_OK": "1"},
    )
    assert "DATABASE_URL" not in env
    assert env.get("CF_OK") == "1"


def test_forbidden_secret_catalog_nonempty():
    assert "DATABASE_URL" in FORBIDDEN_SECRET_NAMES
    assert "AWS_SECRET_ACCESS_KEY" in FORBIDDEN_SECRET_NAMES


def test_boundary_probe_documents_open_fs_and_net(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-appear")
    forbidden = tmp_path / "outside-secret.txt"
    forbidden.write_text("TOP_SECRET_MARKER", encoding="utf-8")
    ws_root = tmp_path / "exec_root"
    ws_root.mkdir()
    ex = ProcessExecutor(
        limits=ExecutionLimits(wall_timeout_seconds=8.0, max_stdout_bytes=64 * 1024),
        workspace_root=ws_root,
        extra_env={"CF_FORBIDDEN_PROBE": str(forbidden)},
    )
    r = ex.run("BOUNDARY_PROBE")
    assert r.status == "succeeded"
    assert r.leaked_pids_after_cleanup == []
    assert r.workspace_cleaned is True
    # Parse probe line from stdout preview
    preview = r.evidence.get("stdout_preview") or ""
    assert "cf_boundary_probe" in preview
    payload = json.loads(preview.split("cf_boundary_probe ", 1)[1].strip())
    assert payload["has_database_url"] is False
    # Current architecture: forbidden path outside workspace is still readable.
    assert payload["forbidden_readable"] is True
    assert payload["network_stack_usable"] is True
    assert payload["can_import_challengeforge"] is True


def test_workspaces_are_unique_per_attempt(tmp_path: Path):
    ex = ProcessExecutor(
        limits=ExecutionLimits(wall_timeout_seconds=5.0),
        workspace_root=tmp_path,
    )
    a = ex.run("LIGHT")
    b = ex.run("LIGHT")
    assert a.workspace != b.workspace
    assert a.workspace_cleaned and b.workspace_cleaned


def test_without_platform_pythonpath_cannot_import_package(tmp_path: Path):
    """Hostile-ready mode would omit PYTHONPATH; workloads.py itself needs it.

    Verify the env builder can omit platform path.
    """
    env = build_execution_env(tmp_path, include_platform_pythonpath=False)
    assert "PYTHONPATH" not in env


def test_repeated_cleanup_idempotent(tmp_path: Path):
    ex = ProcessExecutor(
        limits=ExecutionLimits(wall_timeout_seconds=5.0),
        workspace_root=tmp_path,
    )
    r = ex.run("MANY_FILES")
    path = Path(r.workspace or tmp_path / "x")
    assert ex.cleanup_only(path) is True
    assert ex.cleanup_only(path) is True


@pytest.mark.skipif(os.name != "nt", reason="Job Object ownership on Windows")
def test_containment_still_works_after_boundary_probe(tmp_path: Path):
    import subprocess
    import sys
    import time

    from challengeforge.execution.job_object import JobResourceLimits, create_job

    workloads = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "challengeforge"
        / "execution"
        / "workloads.py"
    )
    job = create_job(JobResourceLimits())
    assert job is not None
    proc = subprocess.Popen(
        [sys.executable, str(workloads), "SLEEP"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    job.assign(proc.pid)
    attempt = new_attempt(
        evaluation_id="boundary-e",
        worker_id="t",
        workload="SLEEP",
        ownership="windows_job_object_kill_on_close",
    )
    attempt.root_pid = proc.pid
    job.close()
    time.sleep(0.4)
    report = contain_attempt(attempt)
    assert report["contained"] is True
    assert not pid_alive(proc.pid)

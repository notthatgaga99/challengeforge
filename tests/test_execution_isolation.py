"""Unit tests for synthetic ProcessExecutor safety invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from challengeforge.execution.limits import ExecutionLimits
from challengeforge.execution.process_executor import ProcessExecutor
from challengeforge.execution.workloads import WORKLOAD_NAMES


@pytest.fixture
def executor(tmp_path: Path) -> ProcessExecutor:
    return ProcessExecutor(
        limits=ExecutionLimits(
            wall_timeout_seconds=3.0,
            max_stdout_bytes=32 * 1024,
            max_stderr_bytes=32 * 1024,
            grace_terminate_seconds=0.5,
        ),
        workspace_root=tmp_path,
    )


def test_workload_catalog_complete():
    assert "TIMEOUT" in WORKLOAD_NAMES
    assert "CHILD_PROCESS" in WORKLOAD_NAMES


def test_light_succeeds(executor: ProcessExecutor):
    r = executor.run("LIGHT")
    assert r.status == "succeeded"
    assert r.exit_code == 0
    assert r.workspace_cleaned is True
    assert r.leaked_pids_after_cleanup == []


def test_failure_nonzero(executor: ProcessExecutor):
    r = executor.run("FAILURE")
    assert r.status == "failed"
    assert r.exit_code == 7
    assert r.workspace_cleaned is True


def test_timeout_containment(executor: ProcessExecutor):
    r = executor.run("TIMEOUT")
    assert r.status == "timeout"
    assert r.wall_ms < 5000
    assert r.leaked_pids_after_cleanup == []
    assert r.workspace_cleaned is True


def test_output_containment(executor: ProcessExecutor):
    r = executor.run("LARGE_OUTPUT")
    assert r.status == "output_limit"
    assert r.stdout_truncated is True
    assert r.stdout_bytes <= 32 * 1024
    assert r.leaked_pids_after_cleanup == []


def test_child_process_tree_cleanup(executor: ProcessExecutor):
    r = executor.run("CHILD_PROCESS")
    assert r.status in ("succeeded", "timeout")
    assert r.leaked_pids_after_cleanup == []
    assert r.process_count_peak >= 1


def test_many_files_workspace_cleaned(executor: ProcessExecutor):
    r = executor.run("MANY_FILES")
    assert r.status == "succeeded"
    assert r.workspace_cleaned is True
    assert r.workspace is not None
    assert not Path(r.workspace).exists()


def test_idempotent_cleanup(executor: ProcessExecutor, tmp_path: Path):
    ws = tmp_path / "orphan"
    ws.mkdir()
    (ws / "a.txt").write_text("x", encoding="utf-8")
    assert executor.cleanup_only(ws) is True
    assert executor.cleanup_only(ws) is True


def test_scrubbed_env_hides_database_url(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "hunter2")
    ex = ProcessExecutor(
        limits=ExecutionLimits(wall_timeout_seconds=5.0),
        workspace_root=tmp_path,
    )
    # Probe via a one-off: run LIGHT and ensure evidence doesn't echo secrets;
    # child env is scrubbed — verified by constructing scrubbed env.
    env = ex._scrubbed_env(tmp_path)
    assert "DATABASE_URL" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "CF_WORKSPACE" in env

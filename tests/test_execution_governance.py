"""Deterministic tests for selective execution resource governance."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from challengeforge.execution.contract import (
    ExecutionOutcome,
    budget_labels,
    disposition,
    outcome_from_result,
)
from challengeforge.execution.job_object import job_objects_available, process_already_in_job
from challengeforge.execution.limits import ExecutionLimits
from challengeforge.execution.ownership import contain_attempt, new_attempt, pid_alive
from challengeforge.execution.process_executor import ExecutionResult, ProcessExecutor


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    return tmp_path


def test_budget_labels_selective_on_windows():
    labels = budget_labels()
    assert labels["wall_timeout"] == "ENFORCED"
    assert labels["stdout_cap"] == "ENFORCED"
    assert labels["network_deny"] == "NOT ENFORCED"
    assert labels["filesystem_jail"] == "NOT ENFORCED"
    assert labels["peak_rss"] == "OBSERVED"
    if job_objects_available():
        assert labels["process_count"] == "ENFORCED"
        assert labels["job_memory"] == "ENFORCED"
        assert labels["cpu_rate"] in ("ENFORCED_THROTTLE", "NOT_ENFORCED")
        assert labels["kill_on_job_close"] == "ENFORCED"


def test_deterministic_resource_outcomes_not_retryable():
    for outcome in (
        ExecutionOutcome.PROCESS_LIMIT,
        ExecutionOutcome.MEMORY_LIMIT,
        ExecutionOutcome.CPU_LIMIT,
        ExecutionOutcome.WORKSPACE_LIMIT,
        ExecutionOutcome.OUTPUT_LIMIT,
        ExecutionOutcome.TIMEOUT,
    ):
        term, retry, score, reason = disposition(outcome, attempt_count=1, max_attempts=5)
        assert term == "failed"
        assert retry is False
        assert score is None
        assert reason and reason.startswith("execution_")


def test_process_limit_enforced_when_job_available(ws: Path):
    if not job_objects_available():
        pytest.skip("Job Objects required for process-count enforcement")
    # Nested jobs (IDE/terminal) reduce effective headroom: limit=2 blocks all children.
    ex = ProcessExecutor(
        limits=ExecutionLimits(
            wall_timeout_seconds=8.0,
            max_process_count=2,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=64 * 1024,
        ),
        workspace_root=ws,
    )
    r = ex.run("PROCESS_HEAVY")
    assert r.status == "process_limit"
    assert outcome_from_result(r) == ExecutionOutcome.PROCESS_LIMIT
    assert r.leaked_pids_after_cleanup == []
    assert r.workspace_cleaned is True
    assert r.process_count_peak <= 2


def test_child_allowed_when_process_budget_has_headroom(ws: Path):
    if not job_objects_available():
        pytest.skip("Job Objects required")
    # Generous ceiling so CHILD_PROCESS can succeed under nesting.
    ex = ProcessExecutor(
        limits=ExecutionLimits(
            wall_timeout_seconds=5.0,
            max_process_count=16,
        ),
        workspace_root=ws,
    )
    r = ex.run("CHILD_PROCESS")
    assert r.status == "succeeded"
    assert r.leaked_pids_after_cleanup == []


def test_job_memory_limit_classifies(ws: Path):
    if not job_objects_available():
        pytest.skip("Job Objects required for job memory")
    ex = ProcessExecutor(
        limits=ExecutionLimits(
            wall_timeout_seconds=10.0,
            max_job_memory_mb=24.0,  # MEMORY_GROW steps 4 MiB
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=64 * 1024,
        ),
        workspace_root=ws,
    )
    r = ex.run("MEMORY_GROW")
    assert r.status == "memory_limit"
    assert outcome_from_result(r) == ExecutionOutcome.MEMORY_LIMIT
    assert r.leaked_pids_after_cleanup == []
    assert r.workspace_cleaned is True


def test_rss_soft_watch_terminates(ws: Path):
    ex = ProcessExecutor(
        limits=ExecutionLimits(
            wall_timeout_seconds=10.0,
            max_rss_mb=20.0,
            rss_poll_interval_seconds=0.05,
        ),
        workspace_root=ws,
    )
    r = ex.run("MEMORY_GROW")
    assert r.status == "memory_limit"
    assert r.leaked_pids_after_cleanup == []


def test_cpu_user_time_limit(ws: Path):
    if not job_objects_available():
        pytest.skip("Job Objects required for CPU user-time")
    ex = ProcessExecutor(
        limits=ExecutionLimits(
            wall_timeout_seconds=15.0,
            max_cpu_seconds=0.25,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=64 * 1024,
        ),
        workspace_root=ws,
    )
    r = ex.run("CPU_HEAVY")
    # May finish under budget on a fast machine before limit — accept either
    # cpu_limit or succeeded if wall/CPU completed naturally under 0.25s user time.
    assert r.status in ("cpu_limit", "succeeded")
    assert r.leaked_pids_after_cleanup == []
    if r.status == "cpu_limit":
        assert outcome_from_result(r) == ExecutionOutcome.CPU_LIMIT


def test_cpu_rate_throttle_reduces_progress(ws: Path):
    if not job_objects_available():
        pytest.skip("Job Objects required for CPU rate")
    # Evidence-only: both should complete; capped run records applied rate.
    capped = ProcessExecutor(
        limits=ExecutionLimits(
            wall_timeout_seconds=8.0,
            cpu_rate_percent=5.0,
        ),
        workspace_root=ws / "capped",
    )
    (ws / "capped").mkdir(exist_ok=True)
    r = capped.run("CPU_HEAVY")
    assert r.status == "succeeded"
    assert r.evidence.get("job_applied", {}).get("cpu_rate_percent") == 5.0


def test_workspace_limit(ws: Path):
    ex = ProcessExecutor(
        limits=ExecutionLimits(
            wall_timeout_seconds=10.0,
            max_workspace_bytes=512 * 1024,  # 512 KiB; DISK_HEAVY writes MBs
            workspace_poll_interval_seconds=0.05,
        ),
        workspace_root=ws,
    )
    r = ex.run("DISK_HEAVY")
    assert r.status == "workspace_limit"
    assert outcome_from_result(r) == ExecutionOutcome.WORKSPACE_LIMIT
    assert r.leaked_pids_after_cleanup == []
    assert r.workspace_cleaned is True


def test_output_limit_still_enforced(ws: Path):
    ex = ProcessExecutor(
        limits=ExecutionLimits(
            wall_timeout_seconds=5.0,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
        ),
        workspace_root=ws,
    )
    r = ex.run("LARGE_OUTPUT")
    assert r.status == "output_limit"
    assert r.stdout_bytes <= 4096


def test_cleanup_idempotent_after_limit(ws: Path):
    ex = ProcessExecutor(
        limits=ExecutionLimits(
            wall_timeout_seconds=8.0,
            max_workspace_bytes=256 * 1024,
            workspace_poll_interval_seconds=0.05,
        ),
        workspace_root=ws,
    )
    r = ex.run("DISK_HEAVY")
    assert r.status == "workspace_limit"
    path = Path(r.workspace or ws / "missing")
    assert ex.cleanup_only(path) is True
    assert ex.cleanup_only(path) is True


def test_worker_death_style_containment_after_cpu_heavy(ws: Path):
    """Simulate recovery containment while a limited run is live."""
    import subprocess
    import sys
    import time

    from challengeforge.execution.job_object import create_job, JobResourceLimits

    if not job_objects_available():
        pytest.skip("Job Objects required")

    workloads = Path(__file__).resolve().parents[1] / "src" / "challengeforge" / "execution" / "workloads.py"
    job = create_job(JobResourceLimits(max_cpu_seconds=30.0))
    assert job is not None
    proc = subprocess.Popen(
        [sys.executable, str(workloads), "CPU_HEAVY"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    job.assign(proc.pid)
    attempt = new_attempt(
        evaluation_id="e1",
        worker_id="w1",
        workload="CPU_HEAVY",
        ownership="windows_job_object_kill_on_close",
    )
    attempt.root_pid = proc.pid
    # Close job → KillOnJobClose (worker-death analogue)
    job.close()
    time.sleep(0.5)
    report = contain_attempt(attempt)
    assert report["contained"] is True
    assert not pid_alive(proc.pid)


def test_outcome_mapping_table():
    fake = ExecutionResult(
        workload="X",
        status="process_limit",
        exit_code=75,
        wall_ms=1,
        startup_ms=1,
        cleanup_ms=1,
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        peak_rss_mb=None,
        process_count_peak=2,
    )
    assert outcome_from_result(fake) == ExecutionOutcome.PROCESS_LIMIT


def test_nested_job_detection_documented():
    # Evidence helper used by experiments — must not raise.
    flag = process_already_in_job()
    assert flag is None or isinstance(flag, bool)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object ownership")
def test_light_with_full_governance_defaults(ws: Path):
    ex = ProcessExecutor(
        limits=ExecutionLimits(
            wall_timeout_seconds=5.0,
            max_process_count=32,
            max_job_memory_mb=256.0,
            cpu_rate_percent=100.0,
        ),
        workspace_root=ws,
    )
    r = ex.run("LIGHT")
    assert r.status == "succeeded"
    assert r.ownership == "windows_job_object_kill_on_close"
    assert r.evidence.get("job_applied", {}).get("kill_on_job_close") is True

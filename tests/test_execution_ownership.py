"""Execution ownership: Job Objects, orphan containment, duplicate prevention."""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from challengeforge.execution.job_object import (
    create_kill_on_close_job,
    job_objects_available,
    ownership_mechanism,
)
from challengeforge.execution.limits import ExecutionLimits
from challengeforge.execution.ownership import (
    ExecutionAttempt,
    contain_attempt,
    new_attempt,
    orphan_suspected,
    pid_alive,
)
from challengeforge.execution.process_executor import ProcessExecutor


def test_ownership_mechanism_label():
    label = ownership_mechanism()
    assert label in {
        "windows_job_object_kill_on_close",
        "posix_process_group",
        "pid_tracking_only",
    }


@pytest.mark.skipif(not job_objects_available(), reason="Windows Job Objects required")
def test_job_object_kills_children_when_handle_closes():
    """KILL_ON_JOB_CLOSE: closing the job handle terminates assigned processes."""
    import subprocess

    job = create_kill_on_close_job()
    assert job is not None
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        job.assign(proc.pid)
        assert pid_alive(proc.pid)
        job.close()
        time.sleep(0.5)
        assert not pid_alive(proc.pid)
    finally:
        if pid_alive(proc.pid):
            proc.kill()
        job.close()


@pytest.mark.skipif(not job_objects_available(), reason="Windows Job Objects required")
def test_executor_with_ownership_reports_mechanism(tmp_path: Path):
    ex = ProcessExecutor(
        limits=ExecutionLimits(wall_timeout_seconds=5.0),
        workspace_root=tmp_path,
    )
    started = {}

    def on_started(info):
        started.update(info)

    r = ex.run("LIGHT", use_os_ownership=True, on_started=on_started)
    assert r.status == "succeeded"
    assert r.ownership == "windows_job_object_kill_on_close"
    assert started.get("root_pid") == r.root_pid
    assert r.root_pid is not None


def test_executor_without_ownership_still_works(tmp_path: Path):
    ex = ProcessExecutor(
        limits=ExecutionLimits(wall_timeout_seconds=5.0),
        workspace_root=tmp_path,
    )
    r = ex.run("LIGHT", use_os_ownership=False)
    assert r.status == "succeeded"
    assert r.ownership == "pid_tracking_only"


def test_contain_attempt_kills_live_process(tmp_path: Path):
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    attempt = new_attempt(
        evaluation_id="e",
        worker_id="w",
        workload="TIMEOUT",
        ownership="pid_tracking_only",
    )
    attempt.root_pid = proc.pid
    assert orphan_suspected(attempt)
    report = contain_attempt(attempt, grace_seconds=0.5)
    assert report["contained"] is True
    assert not pid_alive(proc.pid)


def test_attempt_roundtrip_metadata():
    a = new_attempt(
        evaluation_id="e1",
        worker_id="w1",
        workload="LIGHT",
        ownership="windows_job_object_kill_on_close",
    )
    a.root_pid = 42
    a.workspace = "/tmp/x"
    meta = {"execution_attempt": a.to_metadata()}
    back = ExecutionAttempt.from_metadata(meta)
    assert back is not None
    assert back.execution_attempt_id == a.execution_attempt_id
    assert back.root_pid == 42


def _orphan_child_main(ready_file: str, use_job: bool) -> None:
    """Child process: start a sleep workload under optional Job Object, signal, sleep."""
    import subprocess
    import time
    from pathlib import Path

    from challengeforge.execution.job_object import create_kill_on_close_job

    job = create_kill_on_close_job() if use_job else None
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if job is not None:
        job.assign(proc.pid)
    Path(ready_file).write_text(str(proc.pid), encoding="utf-8")
    # Stay alive holding the job handle.
    time.sleep(60)


@pytest.mark.skipif(not job_objects_available(), reason="Windows Job Objects required")
def test_worker_death_with_job_object_kills_execution(tmp_path: Path):
    ready = tmp_path / "ready.txt"
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=_orphan_child_main, args=(str(ready), True), daemon=True)
    proc.start()
    # Wait for child PID file
    for _ in range(100):
        if ready.exists() and ready.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.05)
    exec_pid = int(ready.read_text(encoding="utf-8").strip())
    assert pid_alive(exec_pid)
    # Hard-kill the "worker" that owns the job handle.
    psutil.Process(proc.pid).kill()
    proc.join(timeout=5)
    # Job Object KillOnJobClose should terminate the execution.
    deadline = time.time() + 3
    while time.time() < deadline and pid_alive(exec_pid):
        time.sleep(0.05)
    assert not pid_alive(exec_pid), "execution should die with job-owning worker"


@pytest.mark.skipif(not job_objects_available(), reason="Windows Job Objects required")
def test_worker_death_without_job_object_can_orphan(tmp_path: Path):
    """Negative control: without Job Object, hard-killed parent can leave orphan."""
    ready = tmp_path / "ready2.txt"
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=_orphan_child_main, args=(str(ready), False), daemon=True)
    proc.start()
    for _ in range(100):
        if ready.exists() and ready.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.05)
    exec_pid = int(ready.read_text(encoding="utf-8").strip())
    assert pid_alive(exec_pid)
    psutil.Process(proc.pid).kill()
    proc.join(timeout=5)
    time.sleep(0.5)
    # May still be alive — that is the ownership gap. Contain explicitly.
    orphaned = pid_alive(exec_pid)
    if orphaned:
        attempt = new_attempt(
            evaluation_id="e", worker_id="w", workload="T", ownership="pid_tracking_only"
        )
        attempt.root_pid = exec_pid
        report = contain_attempt(attempt)
        assert report["contained"] is True
    # Either auto-died (platform quirk) or we contained it — must be dead now.
    assert not pid_alive(exec_pid)

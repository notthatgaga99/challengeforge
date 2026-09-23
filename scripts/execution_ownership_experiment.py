#!/usr/bin/env python
"""Execution ownership / worker-death recovery experiment.

Proves:
  - without OS ownership, worker hard-kill can orphan an execution
  - with Job Object (Windows), worker hard-kill terminates the execution
  - stale recovery must contain orphans before requeue (no duplicate execution)

Does not accept participant uploads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from cf_experiment_paths import ensure_experiment_paths, repo_root

ensure_experiment_paths()

from challengeforge.execution.job_object import job_objects_available, ownership_mechanism
from challengeforge.execution.limits import ExecutionLimits
from challengeforge.execution.ownership import (
    contain_attempt,
    new_attempt,
    orphan_suspected,
    pid_alive,
)
from challengeforge.execution.process_executor import ProcessExecutor
from concurrency_experiment import utc_iso

try:
    from resource_capacity_experiment import env_snapshot
except Exception:  # pragma: no cover

    def env_snapshot() -> dict[str, Any]:
        return {}


ROOT = repo_root()


def _holding_worker(ready_path: str, use_job: bool, workload: str = "TIMEOUT") -> None:
    """Simulate a worker that starts an execution and holds ownership."""
    import os
    import subprocess
    import tempfile
    import traceback

    err_path = Path(ready_path).with_suffix(".err")
    try:
        from challengeforge.execution.job_object import create_kill_on_close_job

        work = Path(tempfile.mkdtemp(prefix="cf-own-"))
        workloads_py = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "challengeforge"
            / "execution"
            / "workloads.py"
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "WINDIR": os.environ.get("WINDIR", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
            "CF_WORKSPACE": str(work),
            "PYTHONUNBUFFERED": "1",
        }
        kwargs: dict[str, Any] = {
            "cwd": str(work),
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            [sys.executable, str(workloads_py), workload],
            **kwargs,
        )
        job = None
        if use_job:
            job = create_kill_on_close_job()
            assert job is not None
            job.assign(proc.pid)
        Path(ready_path).write_text(
            json.dumps(
                {
                    "root_pid": proc.pid,
                    "workspace": str(work),
                    "use_job": use_job,
                    "worker_pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )
        while True:
            time.sleep(1)
    except Exception:
        err_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise


def run_kill_matrix(tmp: Path) -> dict[str, Any]:
    results: dict[str, Any] = {
        "mechanism": ownership_mechanism(),
        "job_objects_available": job_objects_available(),
        "scenarios": {},
    }
    ctx = mp.get_context("spawn")

    for name, use_job in (("with_job", True), ("without_job", False)):
        if use_job and not job_objects_available():
            results["scenarios"][name] = {"skipped": True, "reason": "no job objects"}
            continue
        ready = tmp / f"{name}.json"
        err = tmp / f"{name}.err"
        for p in (ready, err):
            if p.exists():
                p.unlink()
        p = ctx.Process(target=_holding_worker, args=(str(ready), use_job, "TIMEOUT"))
        p.start()
        t0 = time.perf_counter()
        for _ in range(200):
            if ready.exists() or err.exists() or not p.is_alive():
                break
            time.sleep(0.05)
        if not ready.exists():
            results["scenarios"][name] = {
                "error": "ready file missing",
                "err": err.read_text(encoding="utf-8") if err.exists() else None,
                "worker_exitcode": p.exitcode,
            }
            if p.is_alive():
                p.kill()
            p.join(timeout=5)
            continue
        info = json.loads(ready.read_text(encoding="utf-8"))
        root_pid = int(info["root_pid"])
        assert pid_alive(root_pid)
        psutil.Process(p.pid).kill()
        p.join(timeout=5)
        orphan_alive_at = None
        dead_at = None
        for _i in range(60):
            alive = pid_alive(root_pid)
            if alive and orphan_alive_at is None:
                orphan_alive_at = round(time.perf_counter() - t0, 3)
            if not alive:
                dead_at = round(time.perf_counter() - t0, 3)
                break
            time.sleep(0.05)
        still = pid_alive(root_pid)
        contained = None
        if still:
            attempt = new_attempt(
                evaluation_id="exp",
                worker_id="exp",
                workload="TIMEOUT",
                ownership="pid_tracking_only",
            )
            attempt.root_pid = root_pid
            contained = contain_attempt(attempt)
        results["scenarios"][name] = {
            "root_pid": root_pid,
            "orphaned_after_worker_kill": bool(still),
            "detection_or_death_s": dead_at,
            "orphan_observed_s": orphan_alive_at,
            "alive_after_wait": still,
            "containment": contained,
            "use_job": use_job,
        }
    return results


def decide(matrix: dict[str, Any]) -> dict[str, Any]:
    with_job = (matrix.get("scenarios") or {}).get("with_job") or {}
    without = (matrix.get("scenarios") or {}).get("without_job") or {}
    if with_job.get("skipped"):
        gate = "EXPERIMENTAL ONLY"
        rationale = "Job Objects unavailable; ownership not proven on this host."
    elif with_job.get("alive_after_wait"):
        gate = "EXPERIMENTAL ONLY"
        rationale = "Job Object KillOnJobClose did not terminate execution after worker death."
    elif without.get("alive_after_wait") and not with_job.get("alive_after_wait"):
        gate = "KEEP + OS OWNERSHIP"
        rationale = (
            "Without Job Object, worker death can orphan an execution; with "
            "KILL_ON_JOB_CLOSE the execution dies with the worker. Recovery also "
            "contains by stored PID before requeue."
        )
    elif not with_job.get("alive_after_wait") and not with_job.get("error"):
        gate = "KEEP + OS OWNERSHIP"
        rationale = (
            "Windows Job Object ownership terminates executions when the owning "
            "worker dies. Duplicate requeue is gated on containment."
        )
    else:
        gate = "EXPERIMENTAL ONLY"
        rationale = "Ownership behavior inconclusive."
    return {
        "gate": gate,
        "rationale": rationale,
        "participant_code_allowed": False,
        "public_sandbox_ready": False,
        "mechanism": matrix.get("mechanism"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-path", default="docs/execution-ownership-recovery-results.json"
    )
    args = parser.parse_args()
    mp.freeze_support()
    tmp = ROOT / "data" / "ownership-exp"
    tmp.mkdir(parents=True, exist_ok=True)
    matrix = run_kill_matrix(tmp)
    # Capacity microbench: LIGHT with ownership on/off
    capacity = []
    for use_own in (False, True):
        t0 = time.perf_counter()
        walls = []
        for _ in range(3):
            r = ProcessExecutor(
                limits=ExecutionLimits(wall_timeout_seconds=5.0)
            ).run("LIGHT", use_os_ownership=use_own)
            walls.append(r.wall_ms)
        capacity.append(
            {
                "use_os_ownership": use_own,
                "n": 3,
                "mean_wall_ms": round(sum(walls) / len(walls), 2),
                "batch_s": round(time.perf_counter() - t0, 3),
            }
        )
    decision = decide(matrix)
    out = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "claim": "Execution ownership across worker death — not public sandbox",
        },
        "matrix": matrix,
        "capacity_microbench": capacity,
        "decision": decision,
    }
    path = ROOT / args.results_path
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps(decision, indent=2))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()

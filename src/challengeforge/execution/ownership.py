"""Execution ownership: attempt identity, orphan detection, containment."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil


@dataclass
class ExecutionAttempt:
    """One concrete OS execution bound to an evaluation claim."""

    execution_attempt_id: str
    evaluation_id: str
    worker_id: str
    workload: str
    root_pid: int | None = None
    pgid: int | None = None
    workspace: str | None = None
    ownership: str = "pid_tracking_only"
    started_at_monotonic: float = field(default_factory=time.monotonic)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "execution_attempt_id": self.execution_attempt_id,
            "evaluation_id": self.evaluation_id,
            "worker_id": self.worker_id,
            "workload": self.workload,
            "root_pid": self.root_pid,
            "pgid": self.pgid,
            "workspace": self.workspace,
            "ownership": self.ownership,
        }

    @classmethod
    def from_metadata(cls, data: dict[str, Any] | None) -> ExecutionAttempt | None:
        if not isinstance(data, dict):
            return None
        attempt = data.get("execution_attempt")
        if not isinstance(attempt, dict):
            # tolerate flat embedding
            attempt = data if "execution_attempt_id" in data else None
        if not isinstance(attempt, dict) or not attempt.get("execution_attempt_id"):
            return None
        return cls(
            execution_attempt_id=str(attempt["execution_attempt_id"]),
            evaluation_id=str(attempt.get("evaluation_id") or ""),
            worker_id=str(attempt.get("worker_id") or ""),
            workload=str(attempt.get("workload") or ""),
            root_pid=attempt.get("root_pid"),
            pgid=attempt.get("pgid"),
            workspace=attempt.get("workspace"),
            ownership=str(attempt.get("ownership") or "pid_tracking_only"),
        )


def new_attempt(
    *,
    evaluation_id: str,
    worker_id: str,
    workload: str,
    ownership: str,
) -> ExecutionAttempt:
    return ExecutionAttempt(
        execution_attempt_id=str(uuid.uuid4()),
        evaluation_id=evaluation_id,
        worker_id=worker_id,
        workload=workload,
        ownership=ownership,
    )


def pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if not psutil.pid_exists(pid):
        return False
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, psutil.NoSuchProcess):
        return False


def collect_tree_pids(root_pid: int | None) -> list[int]:
    if root_pid is None or not pid_alive(root_pid):
        return []
    try:
        root = psutil.Process(root_pid)
        pids = [root.pid]
        for child in root.children(recursive=True):
            try:
                pids.append(child.pid)
            except (psutil.Error, psutil.NoSuchProcess):
                pass
        return pids
    except (psutil.Error, psutil.NoSuchProcess):
        return []


def contain_attempt(
    attempt: ExecutionAttempt,
    *,
    grace_seconds: float = 0.5,
) -> dict[str, Any]:
    """Best-effort terminate a prior execution attempt's process tree.

    Returns a report used by recovery before requeue.
    """
    report: dict[str, Any] = {
        "execution_attempt_id": attempt.execution_attempt_id,
        "root_pid": attempt.root_pid,
        "was_alive": pid_alive(attempt.root_pid),
        "killed_pids": [],
        "still_alive": [],
        "workspace_exists": bool(
            attempt.workspace and Path(attempt.workspace).exists()
        ),
        "contained": True,
    }
    if not report["was_alive"] and attempt.root_pid is None:
        report["contained"] = True
        return report

    pids = collect_tree_pids(attempt.root_pid)
    # Also try stored root even if children listing failed mid-flight.
    if attempt.root_pid and attempt.root_pid not in pids and pid_alive(attempt.root_pid):
        pids.append(int(attempt.root_pid))

    procs: list[psutil.Process] = []
    for pid in pids:
        try:
            procs.append(psutil.Process(pid))
        except (psutil.Error, psutil.NoSuchProcess):
            pass

    if os.name == "nt" and attempt.root_pid and pid_alive(attempt.root_pid):
        import subprocess

        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(attempt.root_pid)],
                capture_output=True,
                timeout=max(1.0, grace_seconds + 1.0),
                check=False,
            )
            report["killed_pids"].append(int(attempt.root_pid))
        except Exception as exc:
            report["taskkill_error"] = str(exc)

    for p in procs:
        try:
            p.terminate()
            report["killed_pids"].append(p.pid)
        except (psutil.Error, psutil.NoSuchProcess):
            pass
    gone, alive = psutil.wait_procs(procs, timeout=max(0.05, grace_seconds))
    for p in alive:
        try:
            p.kill()
            report["killed_pids"].append(p.pid)
        except (psutil.Error, psutil.NoSuchProcess):
            pass

    time.sleep(0.05)
    still = [pid for pid in pids if pid_alive(pid)]
    report["still_alive"] = still
    report["contained"] = len(still) == 0
    return report


def orphan_suspected(attempt: ExecutionAttempt | None) -> bool:
    if attempt is None:
        return False
    return pid_alive(attempt.root_pid)

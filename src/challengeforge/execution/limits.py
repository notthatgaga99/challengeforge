"""Explicit execution resource budgets (governance, not sandbox isolation).

Each field documents measurement start, owner, exceedance behavior, and
retryability. Observation must not be confused with enforcement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


Enforcement = Literal["ENFORCED", "OBSERVED", "NOT_ENFORCED", "ENFORCED_THROTTLE"]


@dataclass(frozen=True)
class ExecutionLimits:
    """Budgets for one execution attempt.

    Wall / stdout / stderr / workspace: application-layer (executor).
    Process count / job memory / CPU user-time / CPU rate: Windows Job Object
    when available; otherwise weaker app-layer observation or no-op.
    """

    # --- wall time (app) ---
    # Starts: process start. Owner: ProcessExecutor poll loop.
    # Exceeded → TIMEOUT, terminate tree. Deterministic. Not retryable.
    wall_timeout_seconds: float = 5.0

    # --- output (app) ---
    # Starts: first byte read. Owner: stream cap threads.
    # Exceeded → OUTPUT_LIMIT. Deterministic. Not retryable.
    max_stdout_bytes: int = 256 * 1024
    max_stderr_bytes: int = 256 * 1024

    # --- process count (Job Object ActiveProcessLimit when available) ---
    # Starts: job assignment. Owner: OS job + executor poll.
    # Exceeded → PROCESS_LIMIT (poller) and/or CreateProcess fails (1816).
    # Deterministic. Not retryable. None = no process-count ceiling.
    max_process_count: int | None = None

    # --- job commit memory (Job Object JobMemoryLimit when available) ---
    # Starts: job assignment. Owner: OS.
    # Exceeded → alloc fails / process may die → MEMORY_LIMIT when classified.
    # Deterministic. Not retryable. None = no job memory ceiling.
    max_job_memory_mb: float | None = None

    # --- soft RSS watch (app poll; optional secondary) ---
    # Starts: first poll after start. Owner: ProcessExecutor.
    # Exceeded → MEMORY_LIMIT. Racey between polls. Not retryable.
    # None = observe peak_rss only (no terminate-on-RSS).
    max_rss_mb: float | None = None
    rss_poll_interval_seconds: float = 0.1

    # --- CPU user-time budget (Job Object PerJobUserTimeLimit when available) ---
    # Starts: job assignment. Owner: OS.
    # Exceeded → process terminated → CPU_LIMIT. Deterministic. Not retryable.
    # None = no CPU-time kill budget.
    max_cpu_seconds: float | None = None

    # --- CPU rate hard cap (Job Object CpuRateControl HARD_CAP when available) ---
    # Starts: job assignment. Owner: OS scheduler.
    # Behavior: throttle (not a terminal outcome). ENFORCED_THROTTLE.
    # Percent of machine cycles (1–100). None = no rate cap.
    cpu_rate_percent: float | None = None

    # --- workspace disk (app walk) ---
    # Starts: first workspace poll. Owner: ProcessExecutor.
    # Exceeded → WORKSPACE_LIMIT. Racey (writer can overshoot). Not retryable.
    # None = no workspace byte ceiling.
    max_workspace_bytes: int | None = None
    workspace_poll_interval_seconds: float = 0.15

    # --- cleanup ---
    grace_terminate_seconds: float = 0.5
    # Soft deadline for terminate+rmtree accounting (evidence only).
    cleanup_deadline_seconds: float = 5.0
    workspace_retain_on_failure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def budget_enforcement_matrix(*, job_objects: bool) -> dict[str, Enforcement]:
    """What this host/prototype claims for each budget class."""
    return {
        "wall_timeout": "ENFORCED",
        "stdout_cap": "ENFORCED",
        "stderr_cap": "ENFORCED",
        "process_count": "ENFORCED" if job_objects else "OBSERVED",
        "job_memory": "ENFORCED" if job_objects else "NOT_ENFORCED",
        "rss_soft_watch": "ENFORCED",  # only when max_rss_mb set; else N/A
        "peak_rss": "OBSERVED",
        "cpu_user_time": "ENFORCED" if job_objects else "NOT_ENFORCED",
        "cpu_rate": "ENFORCED_THROTTLE" if job_objects else "NOT_ENFORCED",
        "cpu_accounting": "OBSERVED",
        "workspace_bytes": "ENFORCED",  # app-layer when max_workspace_bytes set
        "process_tree_cleanup": "ENFORCED",
        "workspace_cleanup": "ENFORCED",
        "scrubbed_env": "ENFORCED",
        "kill_on_job_close": "ENFORCED" if job_objects else "NOT_ENFORCED",
        "network_deny": "NOT_ENFORCED",
        "cgroup_cpu_quota": "NOT_ENFORCED",
        "cgroup_memory": "NOT_ENFORCED",
        "filesystem_jail": "NOT_ENFORCED",
    }

"""Resource / I/O limits for a single execution (governance, not isolation)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionLimits:
    """Wall/output/process governance for one run.

    Hard cgroup CPU/memory require OS-specific mechanisms (Linux cgroups,
    Windows Job Objects). This prototype enforces wall time, output bytes,
    process-tree cleanup, and optional soft RSS watch via polling.
    """

    wall_timeout_seconds: float = 5.0
    max_stdout_bytes: int = 256 * 1024
    max_stderr_bytes: int = 256 * 1024
    max_rss_mb: float | None = None  # soft watch; None = disabled
    rss_poll_interval_seconds: float = 0.1
    grace_terminate_seconds: float = 0.5
    workspace_retain_on_failure: bool = False

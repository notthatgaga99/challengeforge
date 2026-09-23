"""Synthetic untrusted-execution plane (prototype).

Not wired to participant submissions. See docs/execution-isolation.md.
"""

from challengeforge.execution.limits import ExecutionLimits
from challengeforge.execution.process_executor import ProcessExecutor, ExecutionResult
from challengeforge.execution.workloads import WORKLOAD_NAMES

__all__ = [
    "ExecutionLimits",
    "ExecutionResult",
    "ProcessExecutor",
    "WORKLOAD_NAMES",
]

"""Windows Job Object helpers for ownership and selective resource limits.

Ownership: JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE terminates members when the last
handle closes (worker death).

Governance (when requested):
- ActiveProcessLimit — hard process-count ceiling (CreateProcess → WinError 1816)
- JobMemoryLimit — commit-charge ceiling (alloc → MemoryError / termination)
- PerJobUserTimeLimit — cumulative user-mode CPU time → OS terminates job
- CpuRateControl HARD_CAP — rate throttle (not a terminal outcome)

Nested-job caveat: if the parent is already in a job (common under IDEs /
terminals), effective ActiveProcessLimit headroom can be smaller than the
configured value. Document measurements; do not invent slack without evidence.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any


# Limit flags (winnt.h)
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# Cpu rate control
JobObjectCpuRateControlInformation = 15
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4

JobObjectExtendedLimitInformation = 9


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ControlFlags", wintypes.DWORD),
        ("CpuRate", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class JobResourceLimits:
    """Optional Job Object resource knobs (in addition to KillOnJobClose)."""

    max_active_processes: int | None = None
    max_job_memory_bytes: int | None = None
    max_cpu_seconds: float | None = None
    cpu_rate_percent: float | None = None


class JobObjectHandle:
    """Holds a Windows Job Object; close() or GC kills members if configured."""

    def __init__(
        self,
        handle: int,
        *,
        name: str | None = None,
        applied: dict[str, Any] | None = None,
    ) -> None:
        self.handle = handle
        self.name = name
        self.applied = applied or {}
        self._closed = False

    def assign(self, pid: int) -> None:
        if self._closed:
            raise RuntimeError("job already closed")
        kernel32 = ctypes.windll.kernel32
        PROCESS_ALL_ACCESS = 0x1F0FFF
        proc = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not proc:
            raise OSError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        try:
            if not kernel32.AssignProcessToJobObject(self.handle, proc):
                raise OSError(
                    f"AssignProcessToJobObject failed: {ctypes.get_last_error()}"
                )
        finally:
            kernel32.CloseHandle(proc)

    def query_peak_job_memory(self) -> int | None:
        if self._closed:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        length = wintypes.DWORD()
        ok = ctypes.windll.kernel32.QueryInformationJobObject(
            self.handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(length),
        )
        if not ok:
            return None
        return int(info.PeakJobMemoryUsed)

    def close(self) -> None:
        if self._closed:
            return
        ctypes.windll.kernel32.CloseHandle(self.handle)
        self._closed = True

    def __enter__(self) -> JobObjectHandle:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def job_objects_available() -> bool:
    return os.name == "nt" and hasattr(ctypes.windll.kernel32, "CreateJobObjectW")


def create_kill_on_close_job(name: str | None = None) -> JobObjectHandle | None:
    """Backward-compatible: KillOnJobClose only."""
    return create_job(JobResourceLimits(), name=name)


def create_job(
    resources: JobResourceLimits | None = None,
    *,
    name: str | None = None,
) -> JobObjectHandle | None:
    """Create a job with KillOnJobClose plus optional resource limits."""
    if not job_objects_available():
        return None

    resources = resources or JobResourceLimits()
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateJobObjectW(None, name)
    if not handle:
        raise OSError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")

    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    applied: dict[str, Any] = {"kill_on_job_close": True}

    if resources.max_active_processes is not None and resources.max_active_processes > 0:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = int(
            resources.max_active_processes
        )
        applied["max_active_processes"] = int(resources.max_active_processes)

    if resources.max_job_memory_bytes is not None and resources.max_job_memory_bytes > 0:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = int(resources.max_job_memory_bytes)
        applied["max_job_memory_bytes"] = int(resources.max_job_memory_bytes)

    if resources.max_cpu_seconds is not None and resources.max_cpu_seconds > 0:
        flags |= JOB_OBJECT_LIMIT_JOB_TIME
        # 100-nanosecond units
        info.BasicLimitInformation.PerJobUserTimeLimit = int(
            resources.max_cpu_seconds * 10_000_000
        )
        applied["max_cpu_seconds"] = float(resources.max_cpu_seconds)

    info.BasicLimitInformation.LimitFlags = flags
    ok = kernel32.SetInformationJobObject(
        handle,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        err = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(f"SetInformationJobObject(extended) failed: {err}")

    if resources.cpu_rate_percent is not None and resources.cpu_rate_percent > 0:
        rate = max(1, min(100, float(resources.cpu_rate_percent)))
        cpu = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        cpu.ControlFlags = (
            JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        )
        # Cycles per 10,000 cycles (percent * 100)
        cpu.CpuRate = int(rate * 100)
        ok_cpu = kernel32.SetInformationJobObject(
            handle,
            JobObjectCpuRateControlInformation,
            ctypes.byref(cpu),
            ctypes.sizeof(cpu),
        )
        if not ok_cpu:
            # CPU rate is optional; keep job with other limits.
            applied["cpu_rate_error"] = ctypes.get_last_error()
        else:
            applied["cpu_rate_percent"] = rate

    return JobObjectHandle(handle, name=name, applied=applied)


def ownership_mechanism() -> str:
    if job_objects_available():
        return "windows_job_object_kill_on_close"
    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        return "posix_process_group"
    return "pid_tracking_only"


def process_already_in_job() -> bool | None:
    """True if the current process is already associated with some job."""
    if not job_objects_available():
        return None
    flag = wintypes.BOOL()
    ok = ctypes.windll.kernel32.IsProcessInJob(
        ctypes.windll.kernel32.GetCurrentProcess(),
        None,
        ctypes.byref(flag),
    )
    if not ok:
        return None
    return bool(flag.value)

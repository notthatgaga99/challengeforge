"""Windows Job Object helpers for execution ownership.

When JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is set, closing the last handle to the
job (including process death of the owner) terminates all processes in the job.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Any


class JobObjectHandle:
    """Holds a Windows Job Object; close() or GC kills members if configured."""

    def __init__(self, handle: int, *, name: str | None = None) -> None:
        self.handle = handle
        self.name = name
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
    """Create a job that kills all members when the last handle closes."""
    if not job_objects_available():
        return None

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateJobObjectW(None, name)
    if not handle:
        raise OSError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")

    # JOBOBJECT_EXTENDED_LIMIT_INFORMATION layout (simplified via BasicLimit first
    # is insufficient for KillOnJobClose — need extended).
    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

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

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        handle,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        err = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(f"SetInformationJobObject failed: {err}")

    return JobObjectHandle(handle, name=name)


def ownership_mechanism() -> str:
    if job_objects_available():
        return "windows_job_object_kill_on_close"
    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        return "posix_process_group"
    return "pid_tracking_only"

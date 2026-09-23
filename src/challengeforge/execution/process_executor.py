"""Process-level synthetic executor with tree cleanup and resource budgets."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, IO

import psutil

from challengeforge.execution.job_object import (
    JobResourceLimits,
    create_job,
    job_objects_available,
    ownership_mechanism,
)
from challengeforge.execution.limits import ExecutionLimits


@dataclass
class ExecutionResult:
    workload: str
    status: str
    # succeeded | failed | timeout | output_limit | process_limit |
    # memory_limit | cpu_limit | workspace_limit | error
    exit_code: int | None
    wall_ms: float
    startup_ms: float
    cleanup_ms: float
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    peak_rss_mb: float | None
    process_count_peak: int
    peak_workspace_bytes: int | None = None
    leaked_pids_after_cleanup: list[int] = field(default_factory=list)
    workspace: str | None = None
    workspace_cleaned: bool = False
    root_pid: int | None = None
    ownership: str = "pid_tracking_only"
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workload": self.workload,
            "status": self.status,
            "exit_code": self.exit_code,
            "wall_ms": round(self.wall_ms, 3),
            "startup_ms": round(self.startup_ms, 3),
            "cleanup_ms": round(self.cleanup_ms, 3),
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "peak_rss_mb": (
                round(self.peak_rss_mb, 3) if self.peak_rss_mb is not None else None
            ),
            "process_count_peak": self.process_count_peak,
            "peak_workspace_bytes": self.peak_workspace_bytes,
            "leaked_pids_after_cleanup": self.leaked_pids_after_cleanup,
            "workspace": self.workspace,
            "workspace_cleaned": self.workspace_cleaned,
            "root_pid": self.root_pid,
            "ownership": self.ownership,
            "evidence": self.evidence,
        }


class _StreamCap:
    """Background reader that stops after max_bytes."""

    def __init__(self, stream: IO[bytes], max_bytes: int) -> None:
        self.stream = stream
        self.max_bytes = max_bytes
        self.buf = bytearray()
        self.truncated = False
        self._limit_hit = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="cf-exec-stream", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(4096)
                if not chunk:
                    break
                remaining = self.max_bytes - len(self.buf)
                if remaining <= 0:
                    self.truncated = True
                    self._limit_hit.set()
                    continue
                if len(chunk) > remaining:
                    self.buf.extend(chunk[:remaining])
                    self.truncated = True
                    self._limit_hit.set()
                else:
                    self.buf.extend(chunk)
        except Exception:
            pass

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    @property
    def limit_hit(self) -> bool:
        return self._limit_hit.is_set() or self.truncated


def _dir_size_bytes(root: Path) -> int:
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


class ProcessExecutor:
    """Run a synthetic workload in a subprocess with governance limits."""

    def __init__(
        self,
        *,
        limits: ExecutionLimits | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self.limits = limits or ExecutionLimits()
        self.workspace_root = workspace_root

    def run(
        self,
        workload: str,
        *,
        use_os_ownership: bool = True,
        on_started: Callable[[dict[str, Any]], None] | None = None,
    ) -> ExecutionResult:
        limits = self.limits
        t_start = time.perf_counter()
        work_dir = Path(
            tempfile.mkdtemp(
                prefix=f"cf-exec-{workload.lower()}-",
                dir=str(self.workspace_root) if self.workspace_root else None,
            )
        )
        env = self._scrubbed_env(work_dir)
        workloads_py = Path(__file__).with_name("workloads.py")
        cmd = [sys.executable, str(workloads_py), workload]
        kwargs: dict[str, Any] = {
            "cwd": str(work_dir),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True

        job = None
        ownership = "pid_tracking_only"
        job_applied: dict[str, Any] = {}
        if use_os_ownership and job_objects_available():
            try:
                resources = JobResourceLimits(
                    max_active_processes=limits.max_process_count,
                    max_job_memory_bytes=(
                        int(limits.max_job_memory_mb * 1024 * 1024)
                        if limits.max_job_memory_mb is not None
                        else None
                    ),
                    max_cpu_seconds=limits.max_cpu_seconds,
                    cpu_rate_percent=limits.cpu_rate_percent,
                )
                job = create_job(resources)
                ownership = ownership_mechanism()
                if job is not None:
                    job_applied = dict(job.applied)
            except OSError as exc:
                job = None
                ownership = "pid_tracking_only"
                job_applied = {"create_error": str(exc)}
        elif use_os_ownership:
            ownership = ownership_mechanism()

        startup_begin = time.perf_counter()
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except Exception as exc:
            if job is not None:
                job.close()
            self._cleanup_workspace(work_dir, keep=False)
            return ExecutionResult(
                workload=workload,
                status="error",
                exit_code=None,
                wall_ms=(time.perf_counter() - t_start) * 1000.0,
                startup_ms=0.0,
                cleanup_ms=0.0,
                stdout_bytes=0,
                stderr_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
                peak_rss_mb=None,
                process_count_peak=0,
                workspace=str(work_dir),
                workspace_cleaned=True,
                ownership=ownership,
                evidence={"error": str(exc), "job_applied": job_applied},
            )
        startup_ms = (time.perf_counter() - startup_begin) * 1000.0

        assign_error = None
        if job is not None:
            try:
                job.assign(proc.pid)
            except OSError as exc:
                ownership = "pid_tracking_only"
                job.close()
                job = None
                assign_error = str(exc)

        pgid = None
        if os.name != "nt":
            try:
                pgid = os.getpgid(proc.pid)
            except OSError:
                pgid = None

        if on_started is not None:
            try:
                on_started(
                    {
                        "root_pid": proc.pid,
                        "pgid": pgid,
                        "workspace": str(work_dir),
                        "ownership": ownership,
                    }
                )
            except Exception:
                pass

        assert proc.stdout is not None and proc.stderr is not None
        out_cap = _StreamCap(proc.stdout, limits.max_stdout_bytes)
        err_cap = _StreamCap(proc.stderr, limits.max_stderr_bytes)
        out_cap.start()
        err_cap.start()

        parent = psutil.Process(proc.pid)
        peak_rss: float | None = None
        peak_procs = 1
        peak_workspace = 0
        status = "succeeded"
        deadline = time.perf_counter() + max(0.05, limits.wall_timeout_seconds)
        last_rss_poll = 0.0
        last_ws_poll = 0.0
        root_pid = proc.pid
        limit_reason: str | None = None

        try:
            while True:
                now = time.perf_counter()
                if now >= deadline:
                    status = "timeout"
                    limit_reason = "wall_timeout"
                    break
                if out_cap.limit_hit or err_cap.limit_hit:
                    status = "output_limit"
                    limit_reason = "stdout" if out_cap.limit_hit else "stderr"
                    break
                rc = proc.poll()
                if rc is not None:
                    out_cap.join(timeout=1.0)
                    err_cap.join(timeout=1.0)
                    if out_cap.truncated or err_cap.truncated:
                        status = "output_limit"
                        limit_reason = "stdout" if out_cap.truncated else "stderr"
                    else:
                        classified = self._classify_resource_exit(rc, limits)
                        if classified is not None:
                            status, limit_reason = classified
                        elif rc != 0:
                            status = "failed"
                        else:
                            status = "succeeded"
                    break

                # Process-count ceiling (app poll + OS ActiveProcessLimit).
                try:
                    nprocs = 1 + len(parent.children(recursive=True))
                    peak_procs = max(peak_procs, nprocs)
                    if (
                        limits.max_process_count is not None
                        and nprocs >= limits.max_process_count
                    ):
                        status = "process_limit"
                        limit_reason = "active_process_limit"
                        break
                except (psutil.Error, psutil.NoSuchProcess):
                    pass

                if now - last_rss_poll >= limits.rss_poll_interval_seconds:
                    last_rss_poll = now
                    rss = self._tree_rss_mb(parent)
                    if rss is not None:
                        peak_rss = rss if peak_rss is None else max(peak_rss, rss)
                        if (
                            limits.max_rss_mb is not None
                            and rss > limits.max_rss_mb
                        ):
                            status = "memory_limit"
                            limit_reason = "rss_soft_watch"
                            break

                if (
                    limits.max_workspace_bytes is not None
                    and now - last_ws_poll >= limits.workspace_poll_interval_seconds
                ):
                    last_ws_poll = now
                    ws_bytes = _dir_size_bytes(work_dir)
                    peak_workspace = max(peak_workspace, ws_bytes)
                    if ws_bytes > limits.max_workspace_bytes:
                        status = "workspace_limit"
                        limit_reason = "workspace_bytes"
                        break

                time.sleep(0.02)
        finally:
            wall_ms = (time.perf_counter() - t_start) * 1000.0
            c0 = time.perf_counter()
            peak_job_mem = None
            if job is not None:
                try:
                    peak_job_mem = job.query_peak_job_memory()
                except Exception:
                    peak_job_mem = None
            leaked = self._terminate_tree(proc, parent, limits.grace_terminate_seconds)
            if job is not None:
                try:
                    job.close()
                except Exception:
                    pass
            out_cap.join(timeout=1.0)
            err_cap.join(timeout=1.0)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            time.sleep(0.15)
            if peak_rss is None:
                peak_rss = self._tree_rss_mb(parent)
            if peak_workspace == 0 and work_dir.exists():
                peak_workspace = _dir_size_bytes(work_dir)
            exit_code = proc.poll()
            # Re-classify after forced terminate if we already set a limit status.
            if status == "succeeded" and limits.max_cpu_seconds is not None:
                if exit_code not in (0, None) and abs(int(exit_code or 0)) > 1000:
                    status = "cpu_limit"
                    limit_reason = "job_user_time"
            keep_ws = bool(
                limits.workspace_retain_on_failure and status not in ("succeeded",)
            )
            cleaned = self._cleanup_workspace(work_dir, keep=keep_ws)
            cleanup_ms = (time.perf_counter() - c0) * 1000.0

        out = bytes(out_cap.buf)
        err = bytes(err_cap.buf)
        # Workload self-reported resource signals (deterministic corpus).
        combined = (out + err).decode("utf-8", errors="replace")
        if status in ("succeeded", "failed"):
            if "cf_process_limit" in combined:
                status = "process_limit"
                limit_reason = limit_reason or "workload_signal"
            elif "cf_memory_limit" in combined:
                status = "memory_limit"
                limit_reason = limit_reason or "workload_signal"
            elif "cf_workspace_limit" in combined:
                status = "workspace_limit"
                limit_reason = limit_reason or "workload_signal"

        evidence = {
            "stdout_preview": out[:512].decode("utf-8", errors="replace"),
            "stderr_preview": err[:512].decode("utf-8", errors="replace"),
            "pgid": pgid,
            "job_applied": job_applied,
            "limit_reason": limit_reason,
            "peak_job_memory_bytes": peak_job_mem,
            "budgets": limits.to_dict(),
        }
        if assign_error:
            evidence["job_assign_error"] = assign_error
        return ExecutionResult(
            workload=workload,
            status=status,
            exit_code=exit_code,
            wall_ms=wall_ms,
            startup_ms=startup_ms,
            cleanup_ms=cleanup_ms,
            stdout_bytes=len(out),
            stderr_bytes=len(err),
            stdout_truncated=out_cap.truncated,
            stderr_truncated=err_cap.truncated,
            peak_rss_mb=peak_rss,
            process_count_peak=peak_procs,
            peak_workspace_bytes=peak_workspace or None,
            leaked_pids_after_cleanup=leaked,
            workspace=str(work_dir),
            workspace_cleaned=cleaned,
            root_pid=root_pid,
            ownership=ownership,
            evidence=evidence,
        )

    def _classify_resource_exit(
        self,
        rc: int,
        limits: ExecutionLimits,
    ) -> tuple[str, str] | None:
        """Return (status, reason) if exit maps to a resource limit, else None."""
        # NTSTATUS-style / large codes often mean OS job kill (CPU time).
        if limits.max_cpu_seconds is not None and abs(rc) > 1000:
            return "cpu_limit", "job_user_time"
        return None

    def cleanup_only(self, workspace: Path) -> bool:
        """Idempotent workspace cleanup (Invariant 8)."""
        return self._cleanup_workspace(workspace, keep=False)

    def _scrubbed_env(self, work_dir: Path) -> dict[str, str]:
        keep = ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "LANG", "LC_ALL")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        src = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = src
        env["CF_WORKSPACE"] = str(work_dir)
        env["TMPDIR"] = str(work_dir)
        env["TEMP"] = str(work_dir)
        env["TMP"] = str(work_dir)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _tree_rss_mb(self, parent: psutil.Process) -> float | None:
        try:
            total = parent.memory_info().rss
            for child in parent.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except (psutil.Error, psutil.NoSuchProcess):
                    pass
            return total / (1024 * 1024)
        except (psutil.Error, psutil.NoSuchProcess):
            return None

    def _terminate_tree(
        self,
        proc: subprocess.Popen[bytes],
        parent: psutil.Process,
        grace: float,
    ) -> list[int]:
        pids: list[int] = []
        try:
            children = parent.children(recursive=True)
        except (psutil.Error, psutil.NoSuchProcess):
            children = []
        targets = list(children) + [parent]
        for p in targets:
            try:
                pids.append(p.pid)
                p.terminate()
            except (psutil.Error, psutil.NoSuchProcess):
                pass
        if os.name == "nt" and proc.pid:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=max(1.0, grace + 1.0),
                    check=False,
                )
            except Exception:
                pass
        _gone, alive = psutil.wait_procs(targets, timeout=max(0.05, grace))
        for p in alive:
            try:
                p.kill()
            except (psutil.Error, psutil.NoSuchProcess):
                pass
        try:
            proc.wait(timeout=max(0.05, grace))
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        leaked: list[int] = []
        time.sleep(0.05)
        for pid in pids:
            if not psutil.pid_exists(pid):
                continue
            try:
                p = psutil.Process(pid)
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                    try:
                        p.kill()
                    except (psutil.Error, psutil.NoSuchProcess):
                        pass
                    time.sleep(0.02)
                    if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                        leaked.append(pid)
            except (psutil.Error, psutil.NoSuchProcess):
                pass
        return leaked

    def _cleanup_workspace(self, work_dir: Path, *, keep: bool) -> bool:
        if keep:
            return False
        if not work_dir.exists():
            return True
        for attempt in range(5):
            try:
                shutil.rmtree(work_dir, ignore_errors=False)
            except OSError:
                shutil.rmtree(work_dir, ignore_errors=True)
            if not work_dir.exists():
                return True
            time.sleep(0.05 * (attempt + 1))
        shutil.rmtree(work_dir, ignore_errors=True)
        return not work_dir.exists()

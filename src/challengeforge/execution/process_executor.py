"""Process-level synthetic executor with tree cleanup and I/O caps."""

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
from typing import Any, IO

import psutil

from challengeforge.execution.limits import ExecutionLimits


@dataclass
class ExecutionResult:
    workload: str
    status: str  # succeeded | failed | timeout | output_limit | rss_limit | error
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
    leaked_pids_after_cleanup: list[int] = field(default_factory=list)
    workspace: str | None = None
    workspace_cleaned: bool = False
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
            "leaked_pids_after_cleanup": self.leaked_pids_after_cleanup,
            "workspace": self.workspace,
            "workspace_cleaned": self.workspace_cleaned,
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
        self._thread = threading.Thread(target=self._run, name="cf-exec-stream", daemon=True)

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
                    # Drain and discard to avoid blocking the child on a full pipe.
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

    def run(self, workload: str) -> ExecutionResult:
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

        startup_begin = time.perf_counter()
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except Exception as exc:
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
                evidence={"error": str(exc)},
            )
        startup_ms = (time.perf_counter() - startup_begin) * 1000.0

        assert proc.stdout is not None and proc.stderr is not None
        out_cap = _StreamCap(proc.stdout, limits.max_stdout_bytes)
        err_cap = _StreamCap(proc.stderr, limits.max_stderr_bytes)
        out_cap.start()
        err_cap.start()

        parent = psutil.Process(proc.pid)
        peak_rss: float | None = None
        peak_procs = 1
        status = "succeeded"
        deadline = time.perf_counter() + max(0.05, limits.wall_timeout_seconds)
        last_rss_poll = 0.0

        try:
            while True:
                now = time.perf_counter()
                if now >= deadline:
                    status = "timeout"
                    break
                if out_cap.limit_hit or err_cap.limit_hit:
                    status = "output_limit"
                    break
                rc = proc.poll()
                if rc is not None:
                    out_cap.join(timeout=1.0)
                    err_cap.join(timeout=1.0)
                    if out_cap.truncated or err_cap.truncated:
                        status = "output_limit"
                    elif rc != 0:
                        status = "failed"
                    else:
                        status = "succeeded"
                    break
                if limits.max_rss_mb is not None and (
                    now - last_rss_poll >= limits.rss_poll_interval_seconds
                ):
                    last_rss_poll = now
                    rss = self._tree_rss_mb(parent)
                    if rss is not None:
                        peak_rss = rss if peak_rss is None else max(peak_rss, rss)
                        if rss > limits.max_rss_mb:
                            status = "rss_limit"
                            break
                try:
                    peak_procs = max(
                        peak_procs, 1 + len(parent.children(recursive=True))
                    )
                except (psutil.Error, psutil.NoSuchProcess):
                    pass
                time.sleep(0.02)
        finally:
            wall_ms = (time.perf_counter() - t_start) * 1000.0
            c0 = time.perf_counter()
            leaked = self._terminate_tree(proc, parent, limits.grace_terminate_seconds)
            out_cap.join(timeout=1.0)
            err_cap.join(timeout=1.0)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            # Give the OS a beat to release cwd handles before rmtree (esp. Windows).
            time.sleep(0.15)
            if peak_rss is None:
                peak_rss = self._tree_rss_mb(parent)
            exit_code = proc.poll()
            keep_ws = bool(
                limits.workspace_retain_on_failure and status not in ("succeeded",)
            )
            cleaned = self._cleanup_workspace(work_dir, keep=keep_ws)
            cleanup_ms = (time.perf_counter() - c0) * 1000.0

        out = bytes(out_cap.buf)
        err = bytes(err_cap.buf)
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
            leaked_pids_after_cleanup=leaked,
            workspace=str(work_dir),
            workspace_cleaned=cleaned,
            evidence={
                "stdout_preview": out[:512].decode("utf-8", errors="replace"),
                "stderr_preview": err[:512].decode("utf-8", errors="replace"),
            },
        )

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
        # Windows: also ask the OS to kill the whole tree (covers breakaway children).
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

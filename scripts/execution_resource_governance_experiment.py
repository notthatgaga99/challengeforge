"""Execution resource governance experiment (selective Job Object limits).

Profiles:
  probe     — process / memory / CPU / workspace units
  pressure  — mixed concurrency 1/2/4
  capacity  — LIGHT corpus governance off vs on
  interactive — reuse SEI interactive cells (optional, slower)
  full      — probe + pressure + capacity

Does NOT claim sandbox security. Trusted synthetic workloads only.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import platform
import statistics
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from challengeforge.execution.contract import budget_labels  # noqa: E402
from challengeforge.execution.job_object import (  # noqa: E402
    job_objects_available,
    ownership_mechanism,
    process_already_in_job,
)
from challengeforge.execution.limits import ExecutionLimits  # noqa: E402
from challengeforge.execution.process_executor import ProcessExecutor  # noqa: E402


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_meta() -> dict:
    import psutil

    vm = psutil.virtual_memory()
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "cpu_count_logical": os.cpu_count(),
        "total_memory_mb": round(vm.total / (1024 * 1024), 1),
        "available_memory_mb": round(vm.available / (1024 * 1024), 1),
        "job_objects_available": job_objects_available(),
        "ownership_mechanism": ownership_mechanism(),
        "process_already_in_job": process_already_in_job(),
    }


def run_one(workload: str, limits: ExecutionLimits, ws: Path) -> dict:
    ws.mkdir(parents=True, exist_ok=True)
    ex = ProcessExecutor(limits=limits, workspace_root=ws)
    r = ex.run(workload)
    return r.to_dict()


def probe(ws: Path) -> dict:
    out: dict = {"budget_labels": budget_labels()}

    # Process count
    if job_objects_available():
        r = run_one(
            "PROCESS_HEAVY",
            ExecutionLimits(wall_timeout_seconds=8.0, max_process_count=2),
            ws / "proc",
        )
        out["process_limit"] = {
            "status": r["status"],
            "process_count_peak": r["process_count_peak"],
            "leaked": r["leaked_pids_after_cleanup"],
            "enforced": r["status"] == "process_limit",
        }
    else:
        out["process_limit"] = {"enforced": False, "reason": "no_job_objects"}

    # Job memory
    if job_objects_available():
        r = run_one(
            "MEMORY_GROW",
            ExecutionLimits(wall_timeout_seconds=12.0, max_job_memory_mb=24.0),
            ws / "mem",
        )
        out["memory_limit"] = {
            "status": r["status"],
            "peak_rss_mb": r["peak_rss_mb"],
            "leaked": r["leaked_pids_after_cleanup"],
            "enforced": r["status"] == "memory_limit",
        }
    else:
        out["memory_limit"] = {"enforced": False, "reason": "no_job_objects"}

    # CPU user time
    if job_objects_available():
        # Longer burn to exercise PerJobUserTimeLimit termination.
        import subprocess
        from challengeforge.execution.job_object import JobResourceLimits, create_job

        job = create_job(JobResourceLimits(max_cpu_seconds=0.2))
        assert job is not None
        code = "x=0\nfor i in range(80_000_000):\n x=(x+i)&0xffffffff\nprint('done',x)"
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        job.assign(proc.pid)
        t0 = time.perf_counter()
        cout, cerr = proc.communicate(timeout=30)
        job.close()
        out["cpu_user_time"] = {
            "status": "cpu_limit" if b"done" not in cout else "succeeded",
            "wall_ms": round((time.perf_counter() - t0) * 1000, 3),
            "exit_code": proc.returncode,
            "enforced_terminal": b"done" not in cout,
            "note": "long burn under PerJobUserTimeLimit=0.2s",
            "stderr_preview": cerr[:200].decode("utf-8", errors="replace"),
        }
        # CPU rate throttle evidence
        uncapped = run_one(
            "CPU_HEAVY",
            ExecutionLimits(wall_timeout_seconds=8.0),
            ws / "cpu_u",
        )
        capped = run_one(
            "CPU_HEAVY",
            ExecutionLimits(wall_timeout_seconds=8.0, cpu_rate_percent=5.0),
            ws / "cpu_c",
        )
        out["cpu_rate_throttle"] = {
            "uncapped_wall_ms": uncapped["wall_ms"],
            "capped_wall_ms": capped["wall_ms"],
            "capped_applied": (capped.get("evidence") or {}).get("job_applied", {}),
            "enforced_throttle": bool(
                (capped.get("evidence") or {})
                .get("job_applied", {})
                .get("cpu_rate_percent")
            ),
        }
    else:
        out["cpu_user_time"] = {"enforced_terminal": False}
        out["cpu_rate_throttle"] = {"enforced_throttle": False}

    # Workspace
    r = run_one(
        "DISK_HEAVY",
        ExecutionLimits(
            wall_timeout_seconds=12.0,
            max_workspace_bytes=400_000,
            workspace_poll_interval_seconds=0.05,
        ),
        ws / "disk",
    )
    out["workspace_limit"] = {
        "status": r["status"],
        "peak_workspace_bytes": r["peak_workspace_bytes"],
        "leaked": r["leaked_pids_after_cleanup"],
        "enforced": r["status"] == "workspace_limit",
    }

    # Output (baseline)
    r = run_one(
        "LARGE_OUTPUT",
        ExecutionLimits(wall_timeout_seconds=5.0, max_stdout_bytes=8192),
        ws / "out",
    )
    out["output_limit"] = {
        "status": r["status"],
        "stdout_bytes": r["stdout_bytes"],
        "enforced": r["status"] == "output_limit",
    }

    # Ownership + resource: kill-on-close still works with limits applied
    if job_objects_available():
        from challengeforge.execution.job_object import JobResourceLimits, create_job
        from challengeforge.execution.ownership import contain_attempt, new_attempt, pid_alive
        import subprocess

        job = create_job(
            JobResourceLimits(max_active_processes=8, max_job_memory_bytes=256 * 1024 * 1024)
        )
        assert job is not None
        workloads = ROOT / "src" / "challengeforge" / "execution" / "workloads.py"
        proc = subprocess.Popen(
            [sys.executable, str(workloads), "SLEEP"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        job.assign(proc.pid)
        attempt = new_attempt(
            evaluation_id="gov-race",
            worker_id="exp",
            workload="SLEEP",
            ownership=ownership_mechanism(),
        )
        attempt.root_pid = proc.pid
        job.close()
        time.sleep(0.4)
        contained = contain_attempt(attempt)
        out["ownership_with_limits"] = {
            "alive_after_job_close": pid_alive(proc.pid),
            "containment": contained,
            "ok": (not pid_alive(proc.pid)) and contained.get("contained"),
        }

    return out


def pressure(ws: Path, concurrencies: list[int] | None = None) -> list[dict]:
    concurrencies = concurrencies or [1, 2, 4]
    mix = ["LIGHT", "CPU_HEAVY", "PROCESS_HEAVY", "DISK_HEAVY"]
    limits = ExecutionLimits(
        wall_timeout_seconds=8.0,
        max_process_count=16,
        max_job_memory_mb=128.0,
        max_workspace_bytes=2_000_000,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=64 * 1024,
    )
    cells = []
    for conc in concurrencies:
        t0 = time.perf_counter()
        # Serial batches approximating concurrency via thread pool would be
        # heavier; keep deterministic sequential rounds of `conc` runs.
        results = []
        for i in range(conc):
            wl = mix[i % len(mix)]
            results.append(run_one(wl, limits, ws / f"p{conc}_{i}_{wl}"))
        batch_s = time.perf_counter() - t0
        cells.append(
            {
                "concurrency": conc,
                "batch_s": round(batch_s, 3),
                "statuses": [r["status"] for r in results],
                "leaked_any": any(r["leaked_pids_after_cleanup"] for r in results),
                "mean_wall_ms": round(
                    statistics.mean(r["wall_ms"] for r in results), 2
                ),
            }
        )
    return cells


def capacity(ws: Path) -> list[dict]:
    rows = []
    for gov_on in (False, True):
        limits = (
            ExecutionLimits(
                wall_timeout_seconds=8.0,
                max_process_count=32,
                max_job_memory_mb=256.0,
                cpu_rate_percent=100.0,
            )
            if gov_on
            else ExecutionLimits(wall_timeout_seconds=8.0)
        )
        walls = []
        t0 = time.perf_counter()
        for i in range(3):
            r = run_one("LIGHT", limits, ws / f"cap_{gov_on}_{i}")
            walls.append(r["wall_ms"])
            assert not r["leaked_pids_after_cleanup"]
        rows.append(
            {
                "governance": gov_on,
                "n": 3,
                "mean_wall_ms": round(statistics.mean(walls), 2),
                "batch_s": round(time.perf_counter() - t0, 3),
            }
        )
    return rows


def decide(results: dict) -> dict:
    probe = results.get("probe") or {}
    labels = probe.get("budget_labels") or budget_labels()
    enforced_selective = (
        labels.get("process_count") == "ENFORCED"
        or labels.get("job_memory") == "ENFORCED"
    )
    process_ok = (probe.get("process_limit") or {}).get("enforced")
    memory_ok = (probe.get("memory_limit") or {}).get("enforced")
    workspace_ok = (probe.get("workspace_limit") or {}).get("enforced")
    output_ok = (probe.get("output_limit") or {}).get("enforced")
    ownership_ok = (probe.get("ownership_with_limits") or {}).get("ok", True)

    if not job_objects_available():
        gate = "KEEP CURRENT WATCHDOG + OBSERVATION"
        rationale = "Job Objects unavailable; retain app-layer wall/output/workspace."
    elif process_ok and memory_ok and workspace_ok and output_ok and ownership_ok:
        gate = "KEEP + SELECTIVE RESOURCE LIMITS"
        rationale = (
            "Windows Job Objects enforce process-count and job-memory ceilings; "
            "CPU rate is a throttle; wall/output/workspace remain app-enforced; "
            "network/FS jail still NOT ENFORCED. Ownership KillOnJobClose still holds."
        )
    elif enforced_selective and (process_ok or memory_ok):
        gate = "KEEP + SELECTIVE RESOURCE LIMITS"
        rationale = "Partial Job Object enforcement verified; gaps remain observed/not enforced."
    else:
        gate = "EXPERIMENTAL ONLY"
        rationale = "Resource probes did not confirm reliable selective enforcement."

    return {
        "gate": gate,
        "rationale": rationale,
        "participant_code_allowed": False,
        "public_sandbox_ready": False,
        "budget_labels": labels,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--profile",
        choices=("probe", "pressure", "capacity", "full"),
        default="full",
    )
    p.add_argument(
        "--results-path",
        default="docs/execution-resource-governance-results.json",
    )
    args = p.parse_args()
    mp.freeze_support()

    ws = ROOT / ".cf_gov_ws"
    ws.mkdir(exist_ok=True)
    output: dict = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_meta(),
            "claim": "Selective resource governance — not public sandbox",
        }
    }
    if args.profile in ("probe", "full"):
        print("probe", flush=True)
        output["probe"] = probe(ws / "probe")
    if args.profile in ("pressure", "full"):
        print("pressure", flush=True)
        output["pressure"] = pressure(ws / "pressure")
    if args.profile in ("capacity", "full"):
        print("capacity", flush=True)
        output["capacity"] = capacity(ws / "capacity")

    output["metadata"]["completed_at"] = utc_iso()
    output["decision"] = decide(output)
    path = ROOT / args.results_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(f"decision={output['decision']['gate']}")


if __name__ == "__main__":
    main()

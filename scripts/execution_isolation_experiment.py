#!/usr/bin/env python
"""Execution-isolation prototype measurements (synthetic workloads only).

Does not execute participant uploads. Measures startup/runtime/cleanup and a
small concurrent capacity envelope on the local host.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from cf_experiment_paths import ensure_experiment_paths, repo_root

ensure_experiment_paths()

from challengeforge.execution.limits import ExecutionLimits
from challengeforge.execution.process_executor import ProcessExecutor
from challengeforge.execution.workloads import WORKLOAD_NAMES
from concurrency_experiment import utc_iso

try:
    from resource_capacity_experiment import env_snapshot
except Exception:  # pragma: no cover

    def env_snapshot() -> dict[str, Any]:
        return {}


ROOT = repo_root()


def run_suite(limits: ExecutionLimits, workspace_root: Path) -> dict[str, Any]:
    ex = ProcessExecutor(limits=limits, workspace_root=workspace_root)
    results: dict[str, Any] = {}
    for name in WORKLOAD_NAMES:
        # TIMEOUT / LARGE_OUTPUT need tighter caps already in limits.
        r = ex.run(name)
        results[name] = r.to_dict()
        print(
            f"  {name}: status={r.status} wall={r.wall_ms:.0f}ms "
            f"out={r.stdout_bytes} leaked={r.leaked_pids_after_cleanup} "
            f"cleaned={r.workspace_cleaned}",
            flush=True,
        )
    return results


def capacity_cell(
    *,
    concurrency: int,
    workload: str,
    limits: ExecutionLimits,
    workspace_root: Path,
    repeats: int,
) -> dict[str, Any]:
    def once(_i: int) -> dict[str, Any]:
        # Slightly longer wall for CHILD spawn settle; suite uses default limits.
        time.sleep(0.05)
        ex = ProcessExecutor(limits=limits, workspace_root=workspace_root)
        return ex.run(workload).to_dict()

    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [pool.submit(once, i) for i in range(repeats)]
        for f in as_completed(futs):
            rows.append(f.result())
    wall = time.perf_counter() - t0
    ok = sum(1 for r in rows if r["status"] in ("succeeded", "failed", "timeout", "output_limit"))
    leaks = sum(len(r["leaked_pids_after_cleanup"]) for r in rows)
    mean_wall = sum(r["wall_ms"] for r in rows) / max(1, len(rows))
    mean_cleanup = sum(r["cleanup_ms"] for r in rows) / max(1, len(rows))
    return {
        "concurrency": concurrency,
        "workload": workload,
        "repeats": repeats,
        "batch_wall_s": round(wall, 3),
        "throughput_per_s": round(len(rows) / max(0.001, wall), 3),
        "mean_wall_ms": round(mean_wall, 2),
        "mean_cleanup_ms": round(mean_cleanup, 2),
        "completed": ok,
        "total_leaked_pids": leaks,
        "statuses": {s: sum(1 for r in rows if r["status"] == s) for s in {r["status"] for r in rows}},
    }


def invariants_from_suite(suite: dict[str, Any]) -> dict[str, bool]:
    return {
        "timeout_containment": suite.get("TIMEOUT", {}).get("status") == "timeout"
        and not suite.get("TIMEOUT", {}).get("leaked_pids_after_cleanup"),
        "process_tree_containment": not suite.get("CHILD_PROCESS", {}).get(
            "leaked_pids_after_cleanup"
        ),
        "output_containment": suite.get("LARGE_OUTPUT", {}).get("status") == "output_limit",
        "filesystem_cleanup": suite.get("MANY_FILES", {}).get("workspace_cleaned") is True,
        "nonzero_exit_observed": suite.get("FAILURE", {}).get("status") == "failed",
        "light_ok": suite.get("LIGHT", {}).get("status") == "succeeded",
    }


def decide(invariants: dict[str, bool], capacity: list[dict[str, Any]]) -> dict[str, Any]:
    all_ok = all(invariants.values())
    leak_total = sum(c.get("total_leaked_pids", 0) for c in capacity)
    # Subprocess+tree cleanup sufficient for synthetic prototype if invariants hold.
    if all_ok and leak_total == 0:
        gate = "KEEP SUBPROCESS"
        rationale = (
            "Synthetic process executor met timeout/tree/output/cleanup invariants "
            "on this host. Soft FS containment and scrubbed env are present; "
            "hard net/cgroup isolation is NOT claimed. Participant code remains forbidden."
        )
    elif all_ok:
        gate = "EXPERIMENTAL ONLY"
        rationale = "Invariants mostly hold but cleanup leaks observed under concurrency."
    else:
        gate = "EXPERIMENTAL ONLY"
        rationale = f"One or more safety invariants failed: {invariants}"
    return {
        "gate": gate,
        "rationale": rationale,
        "participant_code_allowed": False,
        "network_isolation_claimed": False,
        "hard_cgroup_claimed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-path", default="docs/execution-isolation-results.json"
    )
    parser.add_argument("--wall-timeout", type=float, default=3.0)
    parser.add_argument("--max-stdout", type=int, default=32 * 1024)
    parser.add_argument("--profile", choices=("smoke", "full"), default="full")
    args = parser.parse_args()

    limits = ExecutionLimits(
        wall_timeout_seconds=float(args.wall_timeout),
        max_stdout_bytes=int(args.max_stdout),
        max_stderr_bytes=int(args.max_stdout),
        grace_terminate_seconds=0.5,
    )
    # Prefer local temp — OneDrive-backed repo paths can make FS workloads flaky.
    ws = Path(tempfile.mkdtemp(prefix="cf-exec-iso-"))

    print("suite", flush=True)
    suite = run_suite(limits, ws)
    inv = invariants_from_suite(suite)

    capacity: list[dict[str, Any]] = []
    conc_levels = (1, 2) if args.profile == "smoke" else (1, 2, 4)
    for c in conc_levels:
        print(f"capacity concurrency={c}", flush=True)
        capacity.append(
            capacity_cell(
                concurrency=c,
                workload="CPU_HEAVY",
                limits=limits,
                workspace_root=ws,
                repeats=max(c, 2) if args.profile == "smoke" else max(c * 2, 4),
            )
        )
        if args.profile == "full":
            capacity.append(
                capacity_cell(
                    concurrency=c,
                    workload="SLEEP",
                    limits=ExecutionLimits(
                        wall_timeout_seconds=5.0,
                        max_stdout_bytes=limits.max_stdout_bytes,
                        max_stderr_bytes=limits.max_stderr_bytes,
                    ),
                    workspace_root=ws,
                    repeats=max(c * 2, 4),
                )
            )

    decision = decide(inv, capacity)
    output = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "profile": args.profile,
            "claim": "Synthetic execution isolation prototype — not participant code",
            "limits": {
                "wall_timeout_seconds": limits.wall_timeout_seconds,
                "max_stdout_bytes": limits.max_stdout_bytes,
            },
        },
        "suite": suite,
        "invariants": inv,
        "capacity": capacity,
        "decision": decision,
    }
    path = ROOT / args.results_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(f"decision={decision['gate']}")
    print(f"invariants={inv}")


if __name__ == "__main__":
    main()

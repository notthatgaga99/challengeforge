#!/usr/bin/env python
"""Integrate ProcessExecutor with durable evaluations (trusted corpus only).

Profiles:
  smoke     — corpus matrix through worker
  tree      — child-process under orchestration + leak scan
  capacity  — concurrent synthetic_execution jobs
  interactive — A/B/C execution pressure vs interactive reads
  full      — all of the above

Does NOT execute participant uploads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psutil

from cf_experiment_paths import ensure_experiment_paths, repo_root

ensure_experiment_paths()

from challengeforge.config import Settings
from challengeforge.execution.contract import TRUSTED_CORPUS, budget_labels
from challengeforge.main import create_app
from concurrency_experiment import asyncpg_url, choose_port, utc_iso, wait_for_server
from interactive_isolated_profile import clients_for_rate, stop_process
from interactive_profile import DEFAULT_ENDPOINTS, drive, seed_world
from resource_capacity_experiment import ResourceExperiment, env_snapshot

ROOT = repo_root()

CORPUS_CASES = [
    ("LIGHT", "succeeded"),
    ("CPU_HEAVY", "succeeded"),
    ("TIMEOUT", "failed"),
    ("LARGE_OUTPUT", "failed"),
    ("CHILD_PROCESS", "succeeded"),
    ("FAILURE", "failed"),
    ("MANY_FILES", "succeeded"),
]


def _api_main(database_url: str, host: str, port: int, artifact_root: str, ready: Any, overrides: dict) -> None:
    import uvicorn

    kwargs = {
        "database_url": database_url,
        "artifact_root": Path(artifact_root),
        "log_level": "WARNING",
        "request_profiling_enabled": True,
        "evaluation_progressive_mode": "legacy",
        "execution_wall_timeout_seconds": 5.0,
        "evaluation_stale_after_seconds": 30,
    }
    kwargs.update(overrides)
    app = create_app(Settings(**kwargs))
    ready.set()
    uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning")).run()


def _worker_main(database_url: str, artifact_root: str, stop: Any, overrides: dict, worker_count: int = 1) -> None:
    import asyncio as aio

    from challengeforge.persistence.session import create_engine as create_cf_engine
    from challengeforge.worker import EvaluationWorker
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async def _run() -> None:
        base = {
            "database_url": database_url,
            "artifact_root": Path(artifact_root),
            "log_level": "WARNING",
            "evaluation_poll_interval_seconds": 0.05,
            "evaluation_stale_after_seconds": 30,
            "evaluation_max_attempts": 3,
            "evaluation_progressive_mode": "synthetic_execution",
            "execution_wall_timeout_seconds": 5.0,
            "execution_max_stdout_bytes": 32 * 1024,
            "resource_aware_runtime_enabled": True,
            "evaluation_max_workers": max(1, worker_count),
        }
        base.update(overrides)
        settings = Settings(**base)
        engine = create_cf_engine(settings)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workers = [
            EvaluationWorker(settings, worker_id=f"sei-w{i}", session_factory=sessions)
            for i in range(max(1, worker_count))
        ]
        tasks = [aio.create_task(w.run_forever()) for w in workers]
        try:
            while not stop.is_set():
                await aio.sleep(0.05)
        finally:
            for w in workers:
                w.request_stop()
            await aio.gather(*tasks, return_exceptions=True)
            await engine.dispose()

    aio.run(_run())


def start_api(database_url: str, host: str, port: int, artifact_root: Path, overrides: dict):
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(
        target=_api_main,
        args=(database_url, host, port, str(artifact_root), ready, overrides),
        daemon=True,
        name="cf-sei-api",
    )
    proc.start()
    return proc, ready


def start_workers(database_url: str, artifact_root: Path, overrides: dict, worker_count: int = 1):
    ctx = mp.get_context("spawn")
    stop = ctx.Event()
    proc = ctx.Process(
        target=_worker_main,
        args=(database_url, str(artifact_root), stop, overrides, worker_count),
        daemon=True,
        name="cf-sei-workers",
    )
    proc.start()
    return proc, stop


async def submit_execution(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    challenge_id: str,
    workload: str,
    index: int,
) -> str:
    create = await client.post(
        f"/api/v1/challenges/{challenge_id}/submissions",
        headers=experiment.participant_headers[0],
        json={
            "metadata": {
                "title": f"sei-{workload}-{index}",
                "evaluation_mode": "synthetic_execution",
                "execution_workload": workload,
                "workload_class": "light",
            }
        },
    )
    create.raise_for_status()
    sid = create.json()["id"]
    sub = await client.post(
        f"/api/v1/submissions/{sid}/submit",
        headers=experiment.participant_headers[0],
        json={},
    )
    sub.raise_for_status()
    return sid


async def wait_evaluation(
    client: httpx.AsyncClient,
    experiment: ResourceExperiment,
    submission_id: str,
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Wait until terminal; enrich with DB result_metadata (not on public API)."""
    from sqlalchemy import text

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        r = await client.get(
            f"/api/v1/submissions/{submission_id}/evaluation",
            headers=experiment.participant_headers[0],
        )
        if r.status_code == 200:
            body = r.json()
            if body.get("status") in ("succeeded", "failed"):
                async with experiment.db.connect() as conn:
                    row = (
                        await conn.execute(
                            text(
                                """
                                SELECT result_metadata, failure_reason, score, status
                                FROM evaluations
                                WHERE submission_id = CAST(:sid AS uuid)
                                """
                            ),
                            {"sid": submission_id},
                        )
                    ).mappings().first()
                if row:
                    body["result_metadata"] = row["result_metadata"] or {}
                    body["failure_reason"] = row["failure_reason"]
                    body["score"] = row["score"]
                    body["status"] = row["status"]
                return body
        await asyncio.sleep(0.1)
    raise TimeoutError(f"evaluation not terminal for {submission_id}")


async def scan_leaked_python_children(parent_pid: int) -> list[int]:
    leaked: list[int] = []
    try:
        parent = psutil.Process(parent_pid)
        for child in parent.children(recursive=True):
            try:
                cmdline = " ".join(child.cmdline())
                if "challengeforge.execution.workloads" in cmdline or "time.sleep(30)" in cmdline:
                    leaked.append(child.pid)
            except (psutil.Error, psutil.NoSuchProcess):
                pass
    except (psutil.Error, psutil.NoSuchProcess):
        pass
    return leaked


async def run_corpus(experiment: ResourceExperiment, client: httpx.AsyncClient, challenge_id: str) -> dict[str, Any]:
    rows = []
    for i, (workload, expected_status) in enumerate(CORPUS_CASES):
        sid = await submit_execution(experiment, client, challenge_id, workload, i)
        ev = await wait_evaluation(client, experiment, sid, timeout=90.0)
        meta = ev.get("result_metadata") or {}
        rows.append(
            {
                "workload": workload,
                "expected_status": expected_status,
                "status": ev.get("status"),
                "score": ev.get("score"),
                "failure_reason": ev.get("failure_reason"),
                "execution_outcome": meta.get("execution_outcome"),
                "execution": meta.get("execution"),
                "ok": ev.get("status") == expected_status,
            }
        )
        print(
            f"  corpus {workload}: status={ev.get('status')} "
            f"outcome={meta.get('execution_outcome')} ok={ev.get('status') == expected_status}",
            flush=True,
        )
    return {
        "cases": rows,
        "all_ok": all(r["ok"] for r in rows),
        "leaked_pids_total": sum(
            len((r.get("execution") or {}).get("leaked_pids_after_cleanup") or []) for r in rows
        ),
    }


async def run_capacity(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    challenge_id: str,
    *,
    concurrency: int,
    workload: str = "CPU_HEAVY",
) -> dict[str, Any]:
    t0 = time.perf_counter()
    sids = await asyncio.gather(
        *[
            submit_execution(experiment, client, challenge_id, workload, 1000 + concurrency * 10 + i)
            for i in range(concurrency)
        ]
    )
    results = []
    for sid in sids:
        results.append(await wait_evaluation(client, experiment, sid, timeout=120.0))
    wall = time.perf_counter() - t0
    return {
        "concurrency": concurrency,
        "workload": workload,
        "batch_wall_s": round(wall, 3),
        "throughput_per_s": round(len(results) / max(0.001, wall), 3),
        "statuses": {
            s: sum(1 for r in results if r.get("status") == s)
            for s in sorted({r.get("status") for r in results})
        },
        "mean_exec_wall_ms": round(
            sum(((r.get("result_metadata") or {}).get("execution") or {}).get("wall_ms") or 0 for r in results)
            / max(1, len(results)),
            2,
        ),
    }


async def run_interactive_cell(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    *,
    label: str,
    challenge_id: str,
    world: dict,
    exec_concurrency: int,
    interactive_rps: float,
    duration: float,
    server_pid: int,
) -> dict[str, Any]:
    # Pre-enqueue executions so they run during the interactive window.
    sids: list[str] = []
    if exec_concurrency > 0:
        for i in range(exec_concurrency):
            sids.append(
                await submit_execution(
                    experiment, client, challenge_id, "CPU_HEAVY", 5000 + i
                )
            )
        # Also enqueue a timeout + output mix for pressure variety
        if exec_concurrency >= 2:
            sids.append(
                await submit_execution(experiment, client, challenge_id, "TIMEOUT", 5100)
            )

    clients = clients_for_rate(interactive_rps)
    interactive = await drive(
        client,
        experiment,
        world,
        endpoints=[e.strip() for e in DEFAULT_ENDPOINTS.split(",")],
        rate=interactive_rps,
        duration=duration,
        clients=clients,
        server_pid=server_pid,
        http_timeout_seconds=5.0,
        load_generator_note="sei interactive vs execution",
    )
    evals = []
    for sid in sids:
        try:
            evals.append(await wait_evaluation(client, experiment, sid, timeout=90.0))
        except TimeoutError:
            evals.append({"status": "timeout_waiting", "id": sid})

    return {
        "label": label,
        "exec_concurrency": exec_concurrency,
        "interactive": interactive,
        "client_p95_ms": (interactive.get("client_latency_ms") or {}).get("p95_ms"),
        "achieved_rps": interactive.get("achieved_requests_per_second"),
        "evaluations": [
            {
                "status": e.get("status"),
                "outcome": (e.get("result_metadata") or {}).get("execution_outcome"),
            }
            for e in evals
        ],
    }


def decide(results: dict[str, Any]) -> dict[str, Any]:
    corpus_ok = (results.get("corpus") or {}).get("all_ok") is True
    leaks = int((results.get("corpus") or {}).get("leaked_pids_total") or 0)
    tree_leaks = int((results.get("tree") or {}).get("host_leaked_after") or 0)
    interactive = results.get("interactive") or []
    # Soft check: with 0 exec pressure, p95 should be finite; with pressure we record deltas.
    if corpus_ok and leaks == 0 and tree_leaks == 0:
        gate = "KEEP EXECUTION INTEGRATION"
        rationale = (
            "Trusted synthetic_execution composes with durable QUEUED/RUNNING/"
            "SUCCEEDED|FAILED, process-tree cleanup held under orchestration, "
            "and participant code remains forbidden. Network/cgroup still NOT ENFORCED."
        )
    elif corpus_ok:
        gate = "EXPERIMENTAL ONLY"
        rationale = "Corpus ok but process leaks or cleanup issues observed."
    else:
        gate = "EXPERIMENTAL ONLY"
        rationale = "Corpus/orchestration expectations not fully met."
    return {
        "gate": gate,
        "rationale": rationale,
        "participant_code_allowed": False,
        "public_sandbox_ready": False,
        "budget_labels": budget_labels(),
        "interactive_cells": len(interactive),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from pgserver import get_server

    data_dir = ROOT / ".pgserver-sei"
    data_dir.mkdir(exist_ok=True)
    embedded = get_server(str(data_dir))
    database_url = asyncpg_url(embedded.get_uri())
    host, port = "127.0.0.1", choose_port()
    base_url = f"http://{host}:{port}"
    artifact_root = ROOT / "data" / "sei-artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)

    overrides = {
        "evaluation_stale_after_seconds": 45,
        "execution_wall_timeout_seconds": 5.0,
        "execution_max_stdout_bytes": 32 * 1024,
    }
    workers = max(1, args.workers)

    output: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "profile": args.profile,
            "claim": "Synthetic execution evaluation integration — not public code",
            "trusted_corpus": list(TRUSTED_CORPUS.keys()),
            "budget_labels": budget_labels(),
        }
    }

    experiment = ResourceExperiment(database_url, base_url)
    await experiment.prepare_database()
    api_proc, ready = start_api(database_url, host, port, artifact_root, overrides)
    ready.wait(timeout=30)
    await wait_for_server(base_url)
    worker_proc, worker_stop = start_workers(
        database_url, artifact_root, overrides, worker_count=workers
    )
    await asyncio.sleep(0.3)

    try:
        limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
        async with httpx.AsyncClient(
            base_url=base_url, limits=limits, timeout=httpx.Timeout(10.0)
        ) as client:
            world = await seed_world(experiment, client, dataset="small")
            _, challenge_id = await experiment.create_published_challenge(
                client, "sei-exec"
            )

            if args.profile in ("smoke", "full", "tree", "capacity", "interactive"):
                print("corpus", flush=True)
                output["corpus"] = await run_corpus(experiment, client, challenge_id)

            if args.profile in ("tree", "full"):
                print("tree", flush=True)
                before = await scan_leaked_python_children(worker_proc.pid)
                sid = await submit_execution(
                    experiment, client, challenge_id, "CHILD_PROCESS", 9001
                )
                ev = await wait_evaluation(client, experiment, sid)
                await asyncio.sleep(1.0)
                after = await scan_leaked_python_children(worker_proc.pid)
                output["tree"] = {
                    "evaluation_status": ev.get("status"),
                    "execution": (ev.get("result_metadata") or {}).get("execution"),
                    "host_leaked_before": len(before),
                    "host_leaked_after": len(after),
                    "leaked_pids": after,
                }
                print(f"  tree leaked_after={after}", flush=True)

            if args.profile in ("capacity", "full"):
                print("capacity", flush=True)
                caps = []
                for c in (1, 2) if args.profile == "capacity" else (1, 2, 4):
                    # Use enough workers for concurrency
                    caps.append(
                        await run_capacity(
                            experiment, client, challenge_id, concurrency=c
                        )
                    )
                    print(f"  capacity c={c} {caps[-1]}", flush=True)
                output["capacity"] = caps

            if args.profile in ("interactive", "full"):
                print("interactive", flush=True)
                cells = []
                for label, conc in (("A_none", 0), ("B_one", 1), ("C_two", 2)):
                    # Fresh challenge per cell to avoid backlog bleed
                    _, cid = await experiment.create_published_challenge(
                        client, f"sei-int-{label}"
                    )
                    cell = await run_interactive_cell(
                        experiment,
                        client,
                        label=label,
                        challenge_id=cid,
                        world=world,
                        exec_concurrency=conc,
                        interactive_rps=float(args.rps),
                        duration=float(args.duration),
                        server_pid=api_proc.pid,
                    )
                    cells.append(cell)
                    print(
                        f"  {label}: client_p95={cell.get('client_p95_ms')} "
                        f"rps={cell.get('achieved_rps')}",
                        flush=True,
                    )
                output["interactive"] = cells
    finally:
        stop_process(worker_proc, worker_stop)
        stop_process(api_proc)
        await experiment.close()
        output["metadata"]["completed_at"] = utc_iso()
        _ = embedded

    output["decision"] = decide(output)
    path = ROOT / args.results_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(f"decision={output['decision']['gate']}")
    return output


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--profile",
        choices=("smoke", "tree", "capacity", "interactive", "full"),
        default="full",
    )
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--rps", type=float, default=15.0)
    p.add_argument("--duration", type=float, default=4.0)
    p.add_argument(
        "--results-path", default="docs/execution-evaluation-integration-results.json"
    )
    args = p.parse_args()
    mp.freeze_support()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

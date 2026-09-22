#!/usr/bin/env python
"""Resource-pressure experiment: interactive + mixed evaluations under A/B/C.

A) resource runtime disabled (static max workers only)
B) static limits with runtime enabled but frozen concurrency (min=max)
C) adaptive resource-aware runtime (min < max)

Bounded for laptop safety. Does not introduce Redis/infra.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psutil
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from challengeforge.config import Settings
from challengeforge.domain.enums import WorkloadClass
from challengeforge.main import create_app
from challengeforge.worker import EvaluationWorker
from concurrency_experiment import (
    UvicornThread,
    asyncpg_url,
    choose_port,
    percentile,
    utc_iso,
    wait_for_server,
)
from resource_capacity_experiment import ResourceExperiment, env_snapshot

MAX_EVALS = 36
MAX_INTERACTIVE = 80
MAX_DURATION = 45.0


def latency(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "mean_ms": round(sum(values) / len(values), 2) if values else 0.0,
    }


async def interactive_burst(
    client: httpx.AsyncClient,
    experiment: ResourceExperiment,
    challenge_id: str,
    *,
    count: int,
) -> dict[str, Any]:
    count = min(count, MAX_INTERACTIVE)
    latencies: list[float] = []
    errors = 0
    for i in range(count):
        headers = experiment.participant_headers[i % len(experiment.participant_headers)]
        t0 = time.perf_counter()
        try:
            response = await client.get(
                f"/api/v1/challenges/{challenge_id}", headers=headers
            )
            latencies.append((time.perf_counter() - t0) * 1000.0)
            if response.status_code >= 400:
                errors += 1
        except Exception:
            errors += 1
            latencies.append((time.perf_counter() - t0) * 1000.0)
    return {
        "requests": count,
        "errors": errors,
        "latency_ms": latency(latencies),
    }


async def enqueue_mix(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    challenge_id: str,
    *,
    light: int,
    medium: int,
    heavy: int,
) -> list[str]:
    ids: list[str] = []
    mix = (
        [WorkloadClass.LIGHT] * light
        + [WorkloadClass.MEDIUM] * medium
        + [WorkloadClass.HEAVY] * heavy
    )[:MAX_EVALS]
    for index, workload in enumerate(mix):
        body = await experiment.create_submitted(
            client, challenge_id, index=index, workload=workload
        )
        ids.append(body["id"])
    return ids


async def run_mode(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    *,
    name: str,
    settings: Settings,
    light: int,
    medium: int,
    heavy: int,
    interactive_count: int,
) -> dict[str, Any]:
    await experiment.prepare_database()
    _, challenge_id = await experiment.create_published_challenge(client, f"rp-{name}")
    submission_ids = await enqueue_mix(
        experiment, client, challenge_id, light=light, medium=medium, heavy=heavy
    )
    workers = [
        EvaluationWorker(
            settings,
            worker_id=f"rp-{name}-{i}",
            session_factory=experiment.worker_sessions,
        )
        for i in range(max(1, settings.evaluation_max_workers))
    ]
    # Cap actual worker *processes* at 1 loop with shared runtime semantics:
    # use a single worker that respects adaptive max via claim_next.
    worker = workers[0]
    task = asyncio.create_task(worker.run_forever())
    proc = psutil.Process()
    proc.cpu_percent(None)
    t0 = time.perf_counter()
    interactive = await interactive_burst(
        client, experiment, challenge_id, count=interactive_count
    )
    # Drain until idle or timeout.
    await worker.drain(max_idle_rounds=4, timeout_seconds=MAX_DURATION)
    worker.request_stop()
    await asyncio.gather(task, return_exceptions=True)
    elapsed = time.perf_counter() - t0
    cpu = proc.cpu_percent(None)
    rss = proc.memory_info().rss / (1024 * 1024)

    async with experiment.worker_sessions() as session:
        from challengeforge.persistence.repositories import EvaluationRepository

        snap = await EvaluationRepository(session).queue_snapshot()
        runtime = await EvaluationRepository(session).read_runtime_state()
        await session.rollback()

    completed = int(snap["succeeded"]) + int(snap["failed"])
    return {
        "name": name,
        "settings": {
            "resource_aware_runtime_enabled": settings.resource_aware_runtime_enabled,
            "evaluation_min_workers": settings.evaluation_min_workers,
            "evaluation_max_workers": settings.evaluation_max_workers,
            "evaluation_scheduling_policy": settings.evaluation_scheduling_policy,
        },
        "enqueued": len(submission_ids),
        "completed": completed,
        "queued_remaining": snap["queued"],
        "running_remaining": snap["running"],
        "interactive": interactive,
        "elapsed_seconds": round(elapsed, 3),
        "evaluation_throughput_per_second": round(
            completed / elapsed if elapsed else 0.0, 3
        ),
        "process": {"cpu_percent": cpu, "rss_mb": round(rss, 2)},
        "runtime_state": runtime,
        "admission_holds": worker.admission_holds,
        "controller_history_len": (
            len(worker.runtime.controller.history) if worker.runtime.controller else 0
        ),
        "submissions_durable": len(submission_ids),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from pgserver import get_server

    data_dir = ROOT / ".pgserver-resource-runtime"
    data_dir.mkdir(exist_ok=True)
    embedded = get_server(str(data_dir))
    database_url = asyncpg_url(embedded.get_uri())
    port = choose_port()
    base_url = f"http://127.0.0.1:{port}"
    experiment = ResourceExperiment(database_url, base_url)
    await experiment.prepare_database()

    api_settings = Settings(
        database_url=database_url,
        artifact_root=ROOT / "data" / "resource-runtime-artifacts",
        log_level="WARNING",
        request_profiling_enabled=False,
        resource_aware_runtime_enabled=False,
    )
    server = UvicornThread(create_app(api_settings), "127.0.0.1", port)
    server.start()
    await wait_for_server(base_url)

    light, medium, heavy = 12, 8, 4
    interactive_count = 40
    modes = {
        "A_no_controller": Settings(
            database_url=database_url,
            artifact_root=ROOT / "data" / "resource-runtime-artifacts",
            log_level="WARNING",
            evaluation_min_workers=2,
            evaluation_max_workers=2,
            resource_aware_runtime_enabled=False,
            evaluation_scheduling_policy="bounded_light_bypass",
            evaluation_poll_interval_seconds=0.05,
            resource_adjust_cooldown_seconds=0.5,
        ),
        "B_static_limits": Settings(
            database_url=database_url,
            artifact_root=ROOT / "data" / "resource-runtime-artifacts",
            log_level="WARNING",
            evaluation_min_workers=2,
            evaluation_max_workers=2,
            resource_aware_runtime_enabled=True,
            evaluation_scheduling_policy="bounded_light_bypass",
            evaluation_poll_interval_seconds=0.05,
            resource_adjust_cooldown_seconds=0.5,
            resource_cpu_high_percent=95.0,
            resource_memory_hard_mb=800.0,
        ),
        "C_adaptive": Settings(
            database_url=database_url,
            artifact_root=ROOT / "data" / "resource-runtime-artifacts",
            log_level="WARNING",
            evaluation_min_workers=1,
            evaluation_max_workers=3,
            resource_aware_runtime_enabled=True,
            evaluation_scheduling_policy="resource_aware",
            evaluation_poll_interval_seconds=0.05,
            resource_adjust_cooldown_seconds=0.5,
            resource_cpu_low_percent=40.0,
            resource_cpu_high_percent=70.0,
            resource_memory_soft_mb=200.0,
            resource_memory_hard_mb=450.0,
        ),
    }

    output: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "mix": {"light": light, "medium": medium, "heavy": heavy},
            "interactive_requests": interactive_count,
            "hypothesis": (
                "Adaptive controller (C) should keep interactive latency competitive "
                "while still completing mixed evaluations; A may complete faster but "
                "risk higher host pressure; B is a static middle ground."
            ),
        },
        "scenarios": {},
        "scorecard": {},
    }
    try:
        async with experiment.db.connect() as connection:
            output["metadata"]["postgresql_version"] = (
                await connection.execute(text("SHOW server_version"))
            ).scalar_one()
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            for name, settings in modes.items():
                print(f"mode {name}", flush=True)
                # Align worker engine settings pool with experiment DB.
                experiment.worker_settings = settings
                result = await run_mode(
                    experiment,
                    client,
                    name=name,
                    settings=settings,
                    light=light,
                    medium=medium,
                    heavy=heavy,
                    interactive_count=interactive_count,
                )
                output["scenarios"][name] = result
    finally:
        output["metadata"]["completed_at"] = utc_iso()
        server.stop()
        await experiment.close()
        _ = embedded

    # Scorecard: useful work under interactive health.
    for name, sc in output["scenarios"].items():
        inter_p95 = sc["interactive"]["latency_ms"]["p95_ms"]
        output["scorecard"][name] = {
            "completed_evaluations": sc["completed"],
            "evaluation_throughput_per_second": sc["evaluation_throughput_per_second"],
            "interactive_p95_ms": inter_p95,
            "interactive_errors": sc["interactive"]["errors"],
            "cpu_percent": sc["process"]["cpu_percent"],
            "rss_mb": sc["process"]["rss_mb"],
            "admission_holds": sc["admission_holds"],
            "submissions_durable": sc["submissions_durable"],
            "useful_work_proxy": round(
                sc["completed"] / max(1.0, inter_p95 / 100.0), 3
            ),
        }

    path = ROOT / args.results_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-path", default="docs/resource-pressure-experiment-results.json"
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Paired FIFO vs bounded-LIGHT-bypass scheduling experiment.

This is intentionally a policy comparison, not a scheduler framework.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from challengeforge.config import Settings
from challengeforge.domain.enums import WorkloadClass
from challengeforge.main import create_app
from challengeforge.worker import EvaluationWorker
from concurrency_experiment import (
    ExperimentMonitor,
    UvicornThread,
    asyncpg_url,
    choose_port,
    utc_iso,
    wait_for_server,
)
from resource_capacity_experiment import ResourceExperiment, env_snapshot

RESULTS = ROOT / "docs" / "evaluation-scheduling-results.json"
MAX_RUNTIME_SECONDS = 90
WORKERS = 2
MAX_CONCURRENT_HEAVY = 1
BYPASS_LIMIT = 2
REPETITIONS = 2

PATTERN = [
    WorkloadClass.HEAVY,
    WorkloadClass.HEAVY,
    WorkloadClass.LIGHT,
    WorkloadClass.LIGHT,
    WorkloadClass.LIGHT,
    WorkloadClass.MEDIUM,
    WorkloadClass.LIGHT,
    WorkloadClass.HEAVY,
    WorkloadClass.HEAVY,
    WorkloadClass.LIGHT,
    WorkloadClass.LIGHT,
    WorkloadClass.MEDIUM,
    WorkloadClass.LIGHT,
    WorkloadClass.HEAVY,
    WorkloadClass.LIGHT,
    WorkloadClass.MEDIUM,
]


async def run_scenario(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    *,
    policy: str,
    repetition: int,
) -> dict[str, Any]:
    _, challenge_id = await experiment.create_published_challenge(
        client, f"scheduling-{policy}-{repetition}"
    )
    for index, workload in enumerate(PATTERN):
        await experiment.create_submitted(
            client,
            challenge_id,
            index=repetition * 1000 + index,
            workload=workload,
        )

    # Align queue timestamps after API setup while preserving deterministic order.
    async with experiment.worker_sessions() as session:
        await session.execute(
            text(
                """
                WITH ranked AS (
                    SELECT e.id,
                           row_number() OVER (ORDER BY e.created_at, e.id) AS n
                    FROM evaluations e
                    JOIN submissions s ON s.id = e.submission_id
                    WHERE s.challenge_id = :cid
                )
                UPDATE evaluations e
                SET created_at = clock_timestamp()
                    + ranked.n * interval '1 millisecond'
                FROM ranked
                WHERE e.id = ranked.id
                """
            ),
            {"cid": challenge_id},
        )
        await session.commit()

    settings = Settings(
        database_url=experiment.database_url,
        artifact_root=ROOT / "data" / "scheduling-artifacts",
        log_level="WARNING",
        evaluation_poll_interval_seconds=0.02,
        evaluation_fake_work_ms=0,
        evaluation_max_workers=WORKERS,
        evaluation_max_concurrent_heavy=MAX_CONCURRENT_HEAVY,
        evaluation_light_bypass_limit=BYPASS_LIMIT,
        evaluation_scheduling_policy=policy,
    )
    workers = [
        EvaluationWorker(
            settings,
            worker_id=f"{policy}-{repetition}-w{i}",
            session_factory=experiment.worker_sessions,
        )
        for i in range(WORKERS)
    ]
    monitor = ExperimentMonitor(experiment.database_url, interval=0.05)
    monitor_task = asyncio.create_task(monitor.run())
    tasks = [asyncio.create_task(worker.run_forever()) for worker in workers]
    t0 = time.perf_counter()
    try:
        deadline = t0 + MAX_RUNTIME_SECONDS
        while time.perf_counter() < deadline:
            async with experiment.worker_sessions() as session:
                remaining = (
                    await session.execute(
                        text(
                            """
                            SELECT count(*)::int
                            FROM evaluations e
                            JOIN submissions s ON s.id = e.submission_id
                            WHERE s.challenge_id = :cid
                              AND e.status IN ('queued', 'running')
                            """
                        ),
                        {"cid": challenge_id},
                    )
                ).scalar_one()
            if remaining == 0:
                break
            await asyncio.sleep(0.1)
    finally:
        for worker in workers:
            worker.request_stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        monitor.stop_event.set()
        await monitor_task

    elapsed = time.perf_counter() - t0
    metrics = await experiment.evaluation_metrics(challenge_id)
    async with experiment.worker_sessions() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT e.workload_class, e.status, e.created_at,
                           e.started_at, e.completed_at
                    FROM evaluations e
                    JOIN submissions s ON s.id = e.submission_id
                    WHERE s.challenge_id = :cid
                    ORDER BY e.completed_at NULLS LAST, e.id
                    """
                ),
                {"cid": challenge_id},
            )
        ).mappings().all()

    busy_seconds = sum(
        (row["completed_at"] - row["started_at"]).total_seconds()
        for row in rows
        if row["completed_at"] and row["started_at"]
    )
    heavy_incomplete = sum(
        1
        for row in rows
        if row["workload_class"] == "heavy" and row["status"] != "succeeded"
    )
    return {
        "policy": policy,
        "repetition": repetition,
        "workers": WORKERS,
        "bypass_limit": BYPASS_LIMIT if policy == "bounded_light_bypass" else 0,
        "elapsed_seconds": round(elapsed, 3),
        "throughput_per_second": round(metrics["completed"] / elapsed, 3),
        "queue_wait_ms": metrics["queue_wait_ms"],
        "execution_ms": metrics["execution_ms"],
        "by_class": metrics["by_class"],
        "completion_order": [row["workload_class"] for row in rows],
        "worker_utilization_percent": round(
            100 * busy_seconds / (elapsed * WORKERS), 2
        ),
        "resources": monitor.result.summary(),
        "starvation_incidents": heavy_incomplete,
        "completed": metrics["completed"],
    }


async def run() -> dict[str, Any]:
    from pgserver import get_server

    data_dir = ROOT / ".pgserver-scheduling"
    data_dir.mkdir(exist_ok=True)
    embedded = get_server(str(data_dir))
    database_url = asyncpg_url(embedded.get_uri())
    port = choose_port()
    base_url = f"http://127.0.0.1:{port}"
    experiment = ResourceExperiment(database_url, base_url)
    await experiment.prepare_database()
    settings = Settings(
        database_url=database_url,
        artifact_root=ROOT / "data" / "scheduling-artifacts",
        log_level="WARNING",
        evaluation_max_workers=WORKERS,
        evaluation_max_concurrent_heavy=MAX_CONCURRENT_HEAVY,
        evaluation_light_bypass_limit=BYPASS_LIMIT,
    )
    server = UvicornThread(create_app(settings), "127.0.0.1", port)
    server.start()
    await wait_for_server(base_url)

    output: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "database": "embedded PostgreSQL",
            "workers": WORKERS,
            "max_concurrent_heavy": MAX_CONCURRENT_HEAVY,
            "light_bypass_limit": BYPASS_LIMIT,
            "repetitions": REPETITIONS,
            "pattern": [item.value for item in PATTERN],
            "safety": {
                "max_runtime_seconds_per_run": MAX_RUNTIME_SECONDS,
                "max_jobs_per_run": len(PATTERN),
            },
        },
        "runs": [],
    }
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
            for repetition in range(REPETITIONS):
                for policy in ("fifo", "bounded_light_bypass"):
                    print(f"{policy} repetition {repetition + 1}", flush=True)
                    output["runs"].append(
                        await run_scenario(
                            experiment,
                            client,
                            policy=policy,
                            repetition=repetition,
                        )
                    )
    finally:
        output["metadata"]["completed_at"] = utc_iso()
        server.stop()
        await experiment.close()
        _ = embedded

    RESULTS.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = asyncio.run(run())
    print(f"wrote {RESULTS}")
    print(f"runs={len(result['runs'])}")

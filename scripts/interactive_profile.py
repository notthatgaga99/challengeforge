#!/usr/bin/env python
"""Bounded interactive-path profiler for ChallengeForge.

Measures client and server stage timings for representative endpoints under a
clean interactive baseline, dataset-size variants, and evaluation-worker modes.

Does not introduce caching or other optimizations — measurement only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psutil
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from challengeforge.config import Settings
from challengeforge.domain.enums import WorkloadClass
from challengeforge.main import create_app
from challengeforge.persistence.models import (
    EvaluationRow,
    SubmissionRow,
)
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

MAX_DURATION_SECONDS = 15.0
MAX_REQUESTS = 800
MAX_CLIENTS = 80
MAX_QUEUED_FOR_POSITION = 1_200
MAX_DATASET_SUBMISSIONS = 1_200

DEFAULT_ENDPOINTS = (
    "challenge_read,evaluation_status,participant_history,submission_creation"
)


def latency(values: list[float]) -> dict[str, float]:
    return {
        "mean_ms": round(sum(values) / len(values), 2) if values else 0.0,
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": round(max(values), 2) if values else 0.0,
    }


def stage_latency(profiles: list[dict[str, Any]], key: str) -> dict[str, float]:
    return latency([float(item.get(key, 0.0) or 0.0) for item in profiles])


def query_count_summary(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    counts = [int(item.get("query_count", 0) or 0) for item in profiles]
    categories: dict[str, list[int]] = defaultdict(list)
    for item in profiles:
        by_cat = item.get("sql_by_category") or {}
        for name, payload in by_cat.items():
            categories[name].append(int(payload.get("count", 0)))
    return {
        "queries_per_request": {
            "mean": round(sum(counts) / len(counts), 3) if counts else 0.0,
            "p50": percentile([float(c) for c in counts], 0.50),
            "p95": percentile([float(c) for c in counts], 0.95),
            "max": max(counts) if counts else 0,
        },
        "category_counts_p50": {
            name: percentile([float(v) for v in values], 0.50)
            for name, values in categories.items()
        },
    }


async def seed_world(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    *,
    dataset: str,
) -> dict[str, Any]:
    sizes = {
        "small": {"submissions": 12, "queued_extra": 0},
        "realistic": {"submissions": 40, "queued_extra": 100},
        "large": {"submissions": 40, "queued_extra": 1000},
    }
    if dataset not in sizes:
        raise ValueError(f"Unknown dataset size: {dataset}")
    cfg = sizes[dataset]
    submission_count = min(cfg["submissions"], MAX_DATASET_SUBMISSIONS)
    queued_extra = min(cfg["queued_extra"], MAX_QUEUED_FOR_POSITION)

    _, challenge_id = await experiment.create_published_challenge(
        client, f"profile-{dataset}"
    )
    statuses: list[tuple[dict[str, str], str]] = []
    for index in range(submission_count):
        body = await experiment.create_submitted(
            client,
            challenge_id,
            index=index,
            workload=WorkloadClass.LIGHT,
        )
        statuses.append(
            (
                experiment.participant_headers[index % len(experiment.participant_headers)],
                body["id"],
            )
        )

    # Drain seeded evaluations so interactive baseline is not evaluation-bound.
    drain_workers = [
        EvaluationWorker(
            experiment.worker_settings.model_copy(
                update={"evaluation_max_workers": 1, "request_profiling_enabled": False}
            ),
            worker_id=f"profile-seed-{i}",
            session_factory=experiment.worker_sessions,
        )
        for i in range(1)
    ]
    await asyncio.gather(
        *[worker.drain(max_idle_rounds=4, timeout_seconds=90) for worker in drain_workers]
    )

    if queued_extra:
        # Insert durable queued evaluations directly to build position cost cases
        # without waiting for HTTP submit throughput.
        async with experiment.worker_sessions() as session:
            for index in range(queued_extra):
                sid = uuid4()
                eid = uuid4()
                session.add(
                    SubmissionRow(
                        id=sid,
                        challenge_id=UUID(challenge_id),
                        participant_id=UUID(
                            experiment.participant_headers[
                                index % len(experiment.participant_headers)
                            ]["X-User-Id"]
                        ),
                        status="submitted",
                        metadata_json={"source": "profile-queue", "i": index},
                        created_at=__import__(
                            "challengeforge.persistence.mapping", fromlist=["utcnow"]
                        ).utcnow(),
                        updated_at=__import__(
                            "challengeforge.persistence.mapping", fromlist=["utcnow"]
                        ).utcnow(),
                    )
                )
                session.add(
                    EvaluationRow(
                        id=eid,
                        submission_id=sid,
                        status="queued",
                        workload_class="light",
                        created_at=__import__(
                            "challengeforge.persistence.mapping", fromlist=["utcnow"]
                        ).utcnow(),
                        attempt_count=0,
                        result_metadata={},
                    )
                )
            await session.commit()

    probe_submission = statuses[0][1]
    return {
        "challenge_id": challenge_id,
        "statuses": statuses,
        "probe_submission_id": probe_submission,
        "dataset": dataset,
        "submission_count": submission_count,
        "queued_extra": queued_extra,
    }


async def queue_position_bench(
    experiment: ResourceExperiment,
    challenge_id: str,
    depths: list[int],
) -> dict[str, Any]:
    from challengeforge.persistence.mapping import utcnow
    from challengeforge.persistence.repositories import EvaluationRepository

    out: dict[str, Any] = {}
    for depth in depths:
        depth = min(max(depth, 1), MAX_QUEUED_FOR_POSITION)
        async with experiment.worker_sessions() as session:
            await session.execute(
                text(
                    """
                    DELETE FROM evaluations e
                    USING submissions s
                    WHERE e.submission_id = s.id
                      AND s.challenge_id = :cid
                    """
                ),
                {"cid": challenge_id},
            )
            await session.execute(
                text(
                    """
                    DELETE FROM submissions
                    WHERE challenge_id = :cid
                      AND (
                        metadata->>'source' = 'profile-queue-bench'
                        OR metadata->>'source' = 'profile-queue'
                      )
                    """
                ),
                {"cid": challenge_id},
            )
            await session.commit()

        from datetime import timedelta

        base_time = utcnow()
        target_id: UUID | None = None
        async with experiment.worker_sessions() as session:
            for index in range(depth):
                sid = uuid4()
                eid = uuid4()
                created = base_time + timedelta(milliseconds=index)
                session.add(
                    SubmissionRow(
                        id=sid,
                        challenge_id=UUID(challenge_id),
                        participant_id=UUID(
                            experiment.participant_headers[
                                index % len(experiment.participant_headers)
                            ]["X-User-Id"]
                        ),
                        status="submitted",
                        metadata_json={"source": "profile-queue-bench", "i": index},
                        created_at=created,
                        updated_at=created,
                    )
                )
                session.add(
                    EvaluationRow(
                        id=eid,
                        submission_id=sid,
                        status="queued",
                        workload_class="light",
                        created_at=created,
                        attempt_count=0,
                        result_metadata={},
                    )
                )
                if index == depth - 1:
                    target_id = eid
            await session.commit()

        assert target_id is not None
        samples: list[float] = []
        for _ in range(8):
            async with experiment.worker_sessions() as session:
                repo = EvaluationRepository(session)
                t0 = time.perf_counter()
                ahead = await repo.queue_position(target_id)
                samples.append((time.perf_counter() - t0) * 1000.0)
                assert ahead == depth - 1
                await session.rollback()
        explain = None
        async with experiment.worker_sessions() as session:
            plan = (
                await session.execute(
                    text(
                        """
                        EXPLAIN (FORMAT JSON)
                        SELECT count(*)
                        FROM evaluations
                        WHERE status = 'queued'
                          AND (
                            created_at < (
                              SELECT created_at FROM evaluations WHERE id = :eid
                            )
                            OR (
                              created_at = (
                                SELECT created_at FROM evaluations WHERE id = :eid
                              )
                              AND id < :eid
                            )
                          )
                        """
                    ),
                    {"eid": target_id},
                )
            ).scalar_one()
            explain = plan
        out[str(depth)] = {
            "jobs_ahead": depth - 1,
            "latency_ms": latency(samples),
            "explain_json": explain,
        }
    return out


async def drive(
    client: httpx.AsyncClient,
    experiment: ResourceExperiment,
    world: dict[str, Any],
    *,
    endpoints: list[str],
    rate: float,
    duration: float,
    clients: int,
) -> dict[str, Any]:
    total = min(MAX_REQUESTS, max(1, round(rate * duration)))
    semaphore = asyncio.Semaphore(min(clients, MAX_CLIENTS))
    client_latencies: dict[str, list[float]] = defaultdict(list)
    server_profiles: dict[str, list[dict[str, Any]]] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)
    timeouts: dict[str, int] = defaultdict(int)
    statuses: dict[str, int] = defaultdict(int)
    process = psutil.Process()
    process.cpu_percent(None)
    t0 = time.perf_counter()

    async def one(index: int, endpoint: str) -> None:
        async with semaphore:
            headers = experiment.participant_headers[
                index % len(experiment.participant_headers)
            ]
            method = "GET"
            path = f"/api/v1/challenges/{world['challenge_id']}"
            payload = None
            if endpoint == "evaluation_status":
                status_headers, submission_id = world["statuses"][
                    index % len(world["statuses"])
                ]
                headers = status_headers
                path = f"/api/v1/submissions/{submission_id}/evaluation"
            elif endpoint == "participant_history":
                path = f"/api/v1/users/{headers['X-User-Id']}/submissions"
            elif endpoint == "submission_creation":
                method = "POST"
                path = f"/api/v1/challenges/{world['challenge_id']}/submissions"
                headers = {
                    **headers,
                    "Idempotency-Key": f"profile-{index}-{uuid4().hex[:8]}",
                }
                payload = {"metadata": {"source": "interactive-profile"}}

            started = time.perf_counter()
            try:
                response = await client.request(
                    method, path, headers=headers, json=payload
                )
                elapsed = (time.perf_counter() - started) * 1000.0
                client_latencies[endpoint].append(elapsed)
                statuses[str(response.status_code)] += 1
                raw = response.headers.get("X-CF-Profile")
                if raw:
                    server_profiles[endpoint].append(json.loads(raw))
                if response.status_code >= 400:
                    errors[endpoint] += 1
            except httpx.TimeoutException:
                timeouts[endpoint] += 1
                errors[endpoint] += 1
                client_latencies[endpoint].append(
                    (time.perf_counter() - started) * 1000.0
                )
            except Exception:
                errors[endpoint] += 1

    tasks: list[asyncio.Task[None]] = []
    for index in range(total):
        due = t0 + index / rate
        await asyncio.sleep(max(0.0, due - time.perf_counter()))
        endpoint = endpoints[index % len(endpoints)]
        tasks.append(asyncio.create_task(one(index, endpoint)))
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - t0
    cpu = process.cpu_percent(None)
    rss = process.memory_info().rss / (1024 * 1024)

    by_endpoint: dict[str, Any] = {}
    for endpoint in endpoints:
        profiles = server_profiles.get(endpoint, [])
        by_endpoint[endpoint] = {
            "client_latency_ms": latency(client_latencies.get(endpoint, [])),
            "requests": len(client_latencies.get(endpoint, [])),
            "errors": errors.get(endpoint, 0),
            "timeouts": timeouts.get(endpoint, 0),
            "server_profiles_captured": len(profiles),
            "server_total_ms": stage_latency(profiles, "total_ms"),
            "pool_wait_ms": stage_latency(profiles, "pool_wait_ms"),
            "sql_ms": stage_latency(profiles, "sql_ms"),
            "non_db_ms": stage_latency(profiles, "non_db_ms"),
            "identity_ms": stage_latency(profiles, "identity_ms"),
            "queue_position_ms": stage_latency(profiles, "queue_position_ms"),
            "query_counts": query_count_summary(profiles),
        }

    all_client = [v for values in client_latencies.values() for v in values]
    all_server = [p for values in server_profiles.values() for p in values]
    return {
        "attempted": total,
        "elapsed_seconds": round(elapsed, 3),
        "achieved_requests_per_second": round(total / elapsed if elapsed else 0.0, 3),
        "error_count": sum(errors.values()),
        "timeout_count": sum(timeouts.values()),
        "status_codes": dict(statuses),
        "client_latency_ms": latency(all_client),
        "server_total_ms": stage_latency(all_server, "total_ms"),
        "pool_wait_ms": stage_latency(all_server, "pool_wait_ms"),
        "sql_ms": stage_latency(all_server, "sql_ms"),
        "non_db_ms": stage_latency(all_server, "non_db_ms"),
        "query_counts": query_count_summary(all_server),
        "by_endpoint": by_endpoint,
        "client_process": {"cpu_percent": cpu, "rss_mb": round(rss, 2)},
        "load_generator_note": (
            "Client and server share one laptop process/host in this harness"
        ),
    }


async def run_scenario(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    *,
    name: str,
    dataset: str,
    endpoints: list[str],
    rate: float,
    duration: float,
    clients: int,
    workers: int,
) -> dict[str, Any]:
    await experiment.prepare_database()
    world = await seed_world(experiment, client, dataset=dataset)
    worker_objs: list[EvaluationWorker] = []
    worker_tasks: list[asyncio.Task[None]] = []
    if workers > 0:
        settings = experiment.worker_settings.model_copy(
            update={
                "evaluation_max_workers": workers,
                "request_profiling_enabled": False,
            }
        )
        worker_objs = [
            EvaluationWorker(
                settings,
                worker_id=f"profile-{name}-w{i}",
                session_factory=experiment.worker_sessions,
            )
            for i in range(workers)
        ]
        worker_tasks = [
            asyncio.create_task(worker.run_forever()) for worker in worker_objs
        ]
    try:
        interactive = await drive(
            client,
            experiment,
            world,
            endpoints=endpoints,
            rate=rate,
            duration=duration,
            clients=clients,
        )
    finally:
        for worker in worker_objs:
            worker.request_stop()
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)

    # Measure queue-position cost with workers stopped so claims cannot shrink the queue.
    position = await queue_position_bench(
        experiment,
        world["challenge_id"],
        depths=[10, 100, 1000],
    )

    return {
        "name": name,
        "dataset": world,
        "workers": workers,
        "interactive": interactive,
        "queue_position_bench": position,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    rate = min(max(args.rate, 1.0), 120.0)
    duration = min(max(args.duration, 1.0), MAX_DURATION_SECONDS)
    clients = min(max(args.clients, 1), MAX_CLIENTS)
    endpoints = [item.strip() for item in args.endpoints.split(",") if item.strip()]
    datasets = [item.strip() for item in args.datasets.split(",") if item.strip()]
    worker_modes = [int(item) for item in args.worker_modes.split(",")]

    from pgserver import get_server

    data_dir = ROOT / ".pgserver-profile"
    data_dir.mkdir(exist_ok=True)
    embedded = get_server(str(data_dir))
    database_url = asyncpg_url(embedded.get_uri())
    port = choose_port()
    base_url = f"http://127.0.0.1:{port}"
    experiment = ResourceExperiment(database_url, base_url)
    await experiment.prepare_database()
    settings = Settings(
        database_url=database_url,
        artifact_root=ROOT / "data" / "profile-artifacts",
        log_level="WARNING",
        db_pool_size=8,
        db_max_overflow=4,
        evaluation_max_workers=1,
        request_profiling_enabled=True,
    )
    server = UvicornThread(create_app(settings), "127.0.0.1", port)
    server.start()
    await wait_for_server(base_url)

    output: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "python": sys.version.split()[0],
            "database_mode": "embedded PostgreSQL",
            "target_request_rate": rate,
            "duration_seconds": duration,
            "concurrent_clients": clients,
            "endpoints": endpoints,
            "datasets": datasets,
            "worker_modes": worker_modes,
            "db_pool_size": settings.db_pool_size,
            "db_max_overflow": settings.db_max_overflow,
            "request_profiling_enabled": True,
            "safety": {
                "max_duration_seconds": MAX_DURATION_SECONDS,
                "max_requests": MAX_REQUESTS,
                "max_clients": MAX_CLIENTS,
            },
        },
        "scenarios": {},
    }
    try:
        async with experiment.db.connect() as connection:
            output["metadata"]["postgresql_version"] = (
                await connection.execute(text("SHOW server_version"))
            ).scalar_one()
        limits = httpx.Limits(
            max_connections=clients, max_keepalive_connections=min(clients, 40)
        )
        timeout = httpx.Timeout(5.0)
        async with httpx.AsyncClient(
            base_url=base_url, limits=limits, timeout=timeout
        ) as client:
            for dataset in datasets:
                for workers in worker_modes:
                    name = f"{dataset}_workers_{workers}"
                    print(f"scenario {name}", flush=True)
                    output["scenarios"][name] = await run_scenario(
                        experiment,
                        client,
                        name=name,
                        dataset=dataset,
                        endpoints=endpoints,
                        rate=rate,
                        duration=duration,
                        clients=clients,
                        workers=workers,
                    )
    finally:
        output["metadata"]["completed_at"] = utc_iso()
        server.stop()
        await experiment.close()
        _ = embedded

    results_path = ROOT / args.results_path
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"wrote {results_path}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=float, default=40.0)
    parser.add_argument("--duration", type=float, default=6.0)
    parser.add_argument("--clients", type=int, default=40)
    parser.add_argument("--endpoints", default=DEFAULT_ENDPOINTS)
    parser.add_argument("--datasets", default="small,realistic,large")
    parser.add_argument("--worker-modes", default="0,1")
    parser.add_argument(
        "--results-path", default="docs/interactive-path-profile-results.json"
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

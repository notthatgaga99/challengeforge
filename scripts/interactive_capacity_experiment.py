#!/usr/bin/env python
"""Safe interactive-capacity experiment under bounded evaluation pressure.

The harness drives open-loop mixed HTTP traffic against a real uvicorn server
and PostgreSQL while the existing two-worker evaluation budget is idle or busy.
It is deliberately bounded and is not a general-purpose stress tool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import event, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from challengeforge.config import Settings
from challengeforge.domain.enums import WorkloadClass
from challengeforge.main import create_app
from challengeforge.persistence import session as session_module
from challengeforge.worker import EvaluationWorker
from concurrency_experiment import (
    ExperimentMonitor,
    PoolTracker,
    UvicornThread,
    asyncpg_url,
    choose_port,
    percentile,
    utc_iso,
    wait_for_server,
)
from resource_capacity_experiment import ResourceExperiment, env_snapshot

RESULTS_PATH = ROOT / "docs" / "interactive-capacity-results.json"

MAX_DURATION_SECONDS = 20.0
MAX_REQUESTS_PER_SCENARIO = 3_000
MAX_TOTAL_REQUESTS = 12_000
MAX_CLIENTS = 150
MAX_WORKERS = 2
MAX_PRESSURE_EVALUATIONS = 48

DEFAULT_MIX = {
    "challenge_reads": 60,
    "evaluation_status": 15,
    "participant_history": 10,
    "submission_creation": 10,
    "organizer_operations": 5,
}

SCENARIOS = {
    "baseline": {
        "initial_jobs": 0,
        "producer_rate": 0.0,
        "distribution": {"light": 1.0},
    },
    "normal": {
        "initial_jobs": 4,
        "producer_rate": 0.5,
        "distribution": {"light": 0.8, "medium": 0.2},
    },
    "sustained_backlog": {
        "initial_jobs": 24,
        "producer_rate": 2.0,
        "distribution": {"light": 0.5, "medium": 0.25, "heavy": 0.25},
    },
    "burst": {
        "initial_jobs": 48,
        "producer_rate": 0.0,
        "distribution": {"light": 0.75, "medium": 0.15, "heavy": 0.10},
    },
    "mixed": {
        "initial_jobs": 32,
        "producer_rate": 0.0,
        "distribution": {"light": 0.5, "medium": 0.25, "heavy": 0.25},
    },
}


def latency(values: list[float]) -> dict[str, float]:
    return {
        "mean_ms": round(sum(values) / len(values), 2) if values else 0.0,
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": round(max(values), 2) if values else 0.0,
    }


def parse_mix(raw: str) -> dict[str, int]:
    mix = dict(DEFAULT_MIX)
    if raw:
        supplied: dict[str, int] = {}
        for item in raw.split(","):
            name, value = item.split("=", 1)
            if name.strip() not in DEFAULT_MIX:
                raise ValueError(f"Unknown traffic category: {name}")
            supplied[name.strip()] = int(value)
        mix = supplied
    if set(mix) != set(DEFAULT_MIX) or sum(mix.values()) != 100:
        raise ValueError(
            "Traffic mix must specify all five categories and total exactly 100."
        )
    if any(value < 0 for value in mix.values()):
        raise ValueError("Traffic percentages cannot be negative.")
    return mix


def parse_evaluation_mix(raw: str) -> dict[str, float] | None:
    if not raw:
        return None
    distribution: dict[str, float] = {}
    for item in raw.split(","):
        name, value = item.split("=", 1)
        name = name.strip().lower()
        if name not in {"light", "medium", "heavy"}:
            raise ValueError(f"Unknown evaluation workload class: {name}")
        distribution[name] = float(value)
    if set(distribution) != {"light", "medium", "heavy"}:
        raise ValueError("Evaluation mix must specify light, medium, and heavy.")
    total = sum(distribution.values())
    if total <= 0 or any(value < 0 for value in distribution.values()):
        raise ValueError("Evaluation workload weights must be non-negative.")
    return {name: value / total for name, value in distribution.items()}


def pick_workload(rng: random.Random, distribution: dict[str, float]) -> WorkloadClass:
    names = list(distribution)
    selected = rng.choices(names, weights=[distribution[n] for n in names], k=1)[0]
    return WorkloadClass(selected)


@dataclass
class InteractiveObservation:
    category: str
    status: int | None
    latency_ms: float
    timed_out: bool = False
    error: str | None = None


class QueueSampler:
    def __init__(self, experiment: ResourceExperiment, challenge_id: str) -> None:
        self.experiment = experiment
        self.challenge_id = challenge_id
        self.stop_event = asyncio.Event()
        self.samples: list[dict[str, Any]] = []

    async def run(self) -> None:
        while not self.stop_event.is_set():
            async with self.experiment.worker_sessions() as session:
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT
                              count(*) FILTER (WHERE e.status = 'queued')::int queued,
                              count(*) FILTER (WHERE e.status = 'running')::int running,
                              count(*) FILTER (
                                WHERE e.status IN ('succeeded', 'failed')
                              )::int completed,
                              COALESCE(
                                max(EXTRACT(EPOCH FROM (
                                  clock_timestamp() - e.created_at
                                ))) FILTER (WHERE e.status = 'queued'),
                                0
                              )::float oldest_seconds
                            FROM evaluations e
                            JOIN submissions s ON s.id = e.submission_id
                            WHERE s.challenge_id = :cid
                            """
                        ),
                        {"cid": self.challenge_id},
                    )
                ).one()
            self.samples.append(
                {
                    "at": time.perf_counter(),
                    "queued": int(row.queued),
                    "running": int(row.running),
                    "completed": int(row.completed),
                    "oldest_seconds": float(row.oldest_seconds),
                }
            )
            await asyncio.sleep(0.1)

    def summary(self, duration: float) -> dict[str, Any]:
        if not self.samples:
            return {"samples": 0}
        first_completed = self.samples[0]["completed"]
        last_completed = self.samples[-1]["completed"]
        return {
            "samples": len(self.samples),
            "max_queue_depth": max(s["queued"] for s in self.samples),
            "ending_queue_depth": self.samples[-1]["queued"],
            "max_active_workers": max(s["running"] for s in self.samples),
            "max_oldest_queued_age_seconds": round(
                max(s["oldest_seconds"] for s in self.samples), 3
            ),
            "completed_during_window": last_completed - first_completed,
            "evaluation_throughput_per_second": round(
                (last_completed - first_completed) / duration if duration else 0.0,
                3,
            ),
        }


async def seed_interactive_data(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    scenario: str,
    worker_count: int,
) -> tuple[str, str, list[tuple[dict[str, str], str]]]:
    _, browse_challenge = await experiment.create_published_challenge(
        client, f"{scenario}-interactive"
    )
    _, pressure_challenge = await experiment.create_published_challenge(
        client, f"{scenario}-pressure"
    )
    statuses: list[tuple[dict[str, str], str]] = []
    for index in range(12):
        body = await experiment.create_submitted(
            client,
            browse_challenge,
            index=80_000 + index,
            workload=WorkloadClass.LIGHT,
        )
        statuses.append((experiment.participant_headers[index], body["id"]))

    seed_settings = experiment.worker_settings.model_copy(
        update={
            "evaluation_max_workers": worker_count,
            "evaluation_scheduling_policy": "bounded_light_bypass",
        }
    )
    seed_workers = [
        EvaluationWorker(
            seed_settings,
            worker_id=f"{scenario}-seed-{i}",
            session_factory=experiment.worker_sessions,
        )
        for i in range(worker_count)
    ]
    await asyncio.gather(
        *[worker.drain(max_idle_rounds=3, timeout_seconds=30) for worker in seed_workers]
    )
    return browse_challenge, pressure_challenge, statuses


async def enqueue_pressure(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    challenge_id: str,
    *,
    count: int,
    distribution: dict[str, float],
    seed: int,
) -> int:
    rng = random.Random(seed)
    produced = 0
    for index in range(min(count, MAX_PRESSURE_EVALUATIONS)):
        await experiment.create_submitted(
            client,
            challenge_id,
            index=90_000 + seed * 100 + index,
            workload=pick_workload(rng, distribution),
        )
        produced += 1
    return produced


async def pressure_producer(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    challenge_id: str,
    *,
    rate: float,
    distribution: dict[str, float],
    duration: float,
    seed: int,
) -> int:
    if rate <= 0:
        return 0
    rng = random.Random(seed)
    produced = 0
    deadline = time.perf_counter() + duration
    interval = 1.0 / rate
    while time.perf_counter() < deadline and produced < MAX_PRESSURE_EVALUATIONS:
        started = time.perf_counter()
        await experiment.create_submitted(
            client,
            challenge_id,
            index=100_000 + seed * 100 + produced,
            workload=pick_workload(rng, distribution),
        )
        produced += 1
        await asyncio.sleep(max(0.0, interval - (time.perf_counter() - started)))
    return produced


async def drive_interactive(
    client: httpx.AsyncClient,
    experiment: ResourceExperiment,
    *,
    challenge_id: str,
    statuses: list[tuple[dict[str, str], str]],
    target_rate: float,
    duration: float,
    clients: int,
    mix: dict[str, int],
    seed: int,
) -> tuple[list[InteractiveObservation], float]:
    total = min(
        MAX_REQUESTS_PER_SCENARIO,
        max(1, round(target_rate * duration)),
    )
    rng = random.Random(seed)
    categories = list(mix)
    weights = [mix[name] for name in categories]
    semaphore = asyncio.Semaphore(min(clients, MAX_CLIENTS))
    observations: list[InteractiveObservation] = []
    started = time.perf_counter()

    async def one(index: int, category: str) -> None:
        async with semaphore:
            headers = experiment.participant_headers[index % len(experiment.participant_headers)]
            method = "GET"
            path = f"/api/v1/challenges/{challenge_id}"
            payload = None
            if category == "evaluation_status":
                status_headers, submission_id = statuses[index % len(statuses)]
                headers = status_headers
                path = f"/api/v1/submissions/{submission_id}/evaluation"
            elif category == "participant_history":
                participant_id = headers["X-User-Id"]
                path = f"/api/v1/users/{participant_id}/submissions"
            elif category == "submission_creation":
                method = "POST"
                path = f"/api/v1/challenges/{challenge_id}/submissions"
                headers = {
                    **headers,
                    "Idempotency-Key": f"interactive-{seed}-{index}-{uuid4().hex[:6]}",
                }
                payload = {"metadata": {"source": "interactive-capacity"}}
            elif category == "organizer_operations":
                headers = experiment.organizer_headers
                path = (
                    "/api/v1/organizer/evaluation-queue"
                    if index % 2
                    else f"/api/v1/challenges/{challenge_id}/submissions"
                )

            t0 = time.perf_counter()
            try:
                response = await client.request(
                    method, path, headers=headers, json=payload
                )
                observations.append(
                    InteractiveObservation(
                        category=category,
                        status=response.status_code,
                        latency_ms=(time.perf_counter() - t0) * 1000,
                    )
                )
            except httpx.TimeoutException as exc:
                observations.append(
                    InteractiveObservation(
                        category=category,
                        status=None,
                        latency_ms=(time.perf_counter() - t0) * 1000,
                        timed_out=True,
                        error=type(exc).__name__,
                    )
                )
            except Exception as exc:
                observations.append(
                    InteractiveObservation(
                        category=category,
                        status=None,
                        latency_ms=(time.perf_counter() - t0) * 1000,
                        error=type(exc).__name__,
                    )
                )

    tasks: list[asyncio.Task[None]] = []
    for index in range(total):
        due = started + index / target_rate
        await asyncio.sleep(max(0.0, due - time.perf_counter()))
        category = rng.choices(categories, weights=weights, k=1)[0]
        tasks.append(asyncio.create_task(one(index, category)))
    await asyncio.gather(*tasks)
    return observations, time.perf_counter() - started


def summarize_interactive(
    observations: list[InteractiveObservation], elapsed: float
) -> dict[str, Any]:
    successful = [o for o in observations if o.status is not None and 200 <= o.status < 300]
    errors = [o for o in observations if o not in successful]
    by_category: dict[str, list[InteractiveObservation]] = defaultdict(list)
    for observation in observations:
        by_category[observation.category].append(observation)

    def category_summary(items: list[InteractiveObservation]) -> dict[str, Any]:
        ok = [o for o in items if o.status is not None and 200 <= o.status < 300]
        return {
            "requests": len(items),
            "success_rate_percent": round(100 * len(ok) / len(items), 3)
            if items
            else 0.0,
            "timeouts": sum(o.timed_out for o in items),
            "latency": latency([o.latency_ms for o in items]),
        }

    return {
        "attempted": len(observations),
        "successful": len(successful),
        "errors": len(errors),
        "timeouts": sum(o.timed_out for o in observations),
        "success_rate_percent": round(
            100 * len(successful) / len(observations), 3
        )
        if observations
        else 0.0,
        "error_rate_percent": round(100 * len(errors) / len(observations), 3)
        if observations
        else 0.0,
        "elapsed_seconds": round(elapsed, 3),
        "achieved_requests_per_second": round(
            len(observations) / elapsed if elapsed else 0.0, 3
        ),
        "latency": latency([o.latency_ms for o in observations]),
        "status_codes": dict(Counter(str(o.status) for o in observations)),
        "error_types": dict(Counter(o.error for o in errors if o.error)),
        "by_category": {
            name: category_summary(items) for name, items in by_category.items()
        },
    }


async def run_scenario(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    *,
    name: str,
    config: dict[str, Any],
    target_rate: float,
    duration: float,
    clients: int,
    mix: dict[str, int],
    seed: int,
    worker_count: int,
) -> dict[str, Any]:
    await experiment.prepare_database()
    browse_id, pressure_id, statuses = await seed_interactive_data(
        experiment, client, name, worker_count
    )
    initial = await enqueue_pressure(
        experiment,
        client,
        pressure_id,
        count=config["initial_jobs"],
        distribution=config["distribution"],
        seed=seed,
    )

    settings = experiment.worker_settings.model_copy(
        update={
            "evaluation_max_workers": worker_count,
            "evaluation_max_concurrent_heavy": 1,
            "evaluation_light_bypass_limit": 2,
            "evaluation_scheduling_policy": "bounded_light_bypass",
        }
    )
    workers = [
        EvaluationWorker(
            settings,
            worker_id=f"interactive-{name}-{i}",
            session_factory=experiment.worker_sessions,
        )
        for i in range(worker_count)
    ]
    worker_tasks = [asyncio.create_task(worker.run_forever()) for worker in workers]
    monitor = ExperimentMonitor(experiment.database_url, interval=0.05)
    monitor_task = asyncio.create_task(monitor.run())
    queue_sampler = QueueSampler(experiment, pressure_id)
    queue_task = asyncio.create_task(queue_sampler.run())
    if experiment.pool_tracker:
        experiment.pool_tracker.reset()

    producer_task = asyncio.create_task(
        pressure_producer(
            experiment,
            client,
            pressure_id,
            rate=config["producer_rate"],
            distribution=config["distribution"],
            duration=duration,
            seed=seed + 500,
        )
    )
    try:
        observations, elapsed = await drive_interactive(
            client,
            experiment,
            challenge_id=browse_id,
            statuses=statuses,
            target_rate=target_rate,
            duration=duration,
            clients=clients,
            mix=mix,
            seed=seed,
        )
        produced_during = await producer_task
    finally:
        if not producer_task.done():
            producer_task.cancel()
            await asyncio.gather(producer_task, return_exceptions=True)
        queue_sampler.stop_event.set()
        await queue_task
        for worker in workers:
            worker.request_stop()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        monitor.stop_event.set()
        await monitor_task

    evaluation = await experiment.evaluation_metrics(pressure_id)
    resources = monitor.result.summary()
    if experiment.pool_tracker:
        resources.update(experiment.pool_tracker.snapshot())
    return {
        "name": name,
        "pressure": {
            **config,
            "initial_jobs_created": initial,
            "jobs_created_during_window": produced_during,
        },
        "interactive": summarize_interactive(observations, elapsed),
        "evaluation": {
            **queue_sampler.summary(elapsed),
            "completed_total": evaluation["completed"],
            "queue_wait_ms": evaluation["queue_wait_ms"],
            "execution_ms": evaluation["execution_ms"],
            "by_class": evaluation["by_class"],
            "worker_count": worker_count,
        },
        "resources": resources,
        "worker_metrics": {
            "claims": sum(worker.claims for worker in workers),
            "completed": sum(worker.jobs_completed for worker in workers),
            "failed": sum(worker.jobs_failed for worker in workers),
            "peak_active": max((worker.peak_active for worker in workers), default=0),
        },
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    target_rate = min(max(args.rate, 1.0), 150.0)
    duration = min(max(args.duration, 1.0), MAX_DURATION_SECONDS)
    clients = min(max(args.clients, 1), MAX_CLIENTS)
    worker_count = min(max(args.workers, 1), MAX_WORKERS)
    mix = parse_mix(args.mix)
    evaluation_mix = parse_evaluation_mix(args.evaluation_mix)
    requested_per_scenario = round(target_rate * duration)
    if requested_per_scenario > MAX_REQUESTS_PER_SCENARIO:
        duration = MAX_REQUESTS_PER_SCENARIO / target_rate
    scenario_names = args.scenarios.split(",")
    unknown = set(scenario_names) - set(SCENARIOS)
    if unknown:
        raise ValueError(f"Unknown scenarios: {sorted(unknown)}")
    if target_rate * duration * len(scenario_names) > MAX_TOTAL_REQUESTS:
        raise ValueError("Requested experiment exceeds MAX_TOTAL_REQUESTS.")

    from pgserver import get_server

    data_dir = ROOT / ".pgserver-interactive"
    data_dir.mkdir(exist_ok=True)
    embedded = get_server(str(data_dir))
    database_url = asyncpg_url(embedded.get_uri())
    port = choose_port()
    base_url = f"http://127.0.0.1:{port}"
    experiment = ResourceExperiment(database_url, base_url)
    await experiment.prepare_database()
    settings = Settings(
        database_url=database_url,
        artifact_root=ROOT / "data" / "interactive-artifacts",
        log_level="WARNING",
        db_pool_size=8,
        db_max_overflow=4,
        evaluation_max_workers=worker_count,
        evaluation_max_concurrent_heavy=1,
        evaluation_light_bypass_limit=2,
    )
    server = UvicornThread(create_app(settings), "127.0.0.1", port)
    server.start()
    await wait_for_server(base_url)

    assert session_module._engine is not None
    experiment.pool_tracker = PoolTracker()
    event.listen(
        session_module._engine.sync_engine.pool,
        "checkout",
        experiment.pool_tracker.checkout,
    )
    event.listen(
        session_module._engine.sync_engine.pool,
        "checkin",
        experiment.pool_tracker.checkin,
    )
    output: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "python": sys.version.split()[0],
            "database_mode": "embedded PostgreSQL",
            "target_request_rate": target_rate,
            "duration_seconds": duration,
            "concurrent_clients": clients,
            "traffic_mix_percent": mix,
            "worker_count": worker_count,
            "evaluation_workload_scenarios": scenario_names,
            "safety_limits": {
                "max_duration_seconds": MAX_DURATION_SECONDS,
                "max_requests_per_scenario": MAX_REQUESTS_PER_SCENARIO,
                "max_total_requests": MAX_TOTAL_REQUESTS,
                "max_clients": MAX_CLIENTS,
                "max_workers": MAX_WORKERS,
                "max_pressure_evaluations": MAX_PRESSURE_EVALUATIONS,
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
            max_connections=clients,
            max_keepalive_connections=min(clients, 50),
        )
        timeout = httpx.Timeout(5.0)
        async with httpx.AsyncClient(
            base_url=base_url, limits=limits, timeout=timeout
        ) as client:
            for index, name in enumerate(scenario_names):
                print(f"scenario {name}", flush=True)
                scenario_config = dict(SCENARIOS[name])
                if evaluation_mix is not None:
                    scenario_config["distribution"] = evaluation_mix
                output["scenarios"][name] = await run_scenario(
                    experiment,
                    client,
                    name=name,
                    config=scenario_config,
                    target_rate=target_rate,
                    duration=duration,
                    clients=clients,
                    mix=mix,
                    seed=args.seed + index * 1000,
                    worker_count=worker_count,
                )
    finally:
        output["metadata"]["completed_at"] = utc_iso()
        server.stop()
        await experiment.close()
        _ = embedded

    results_path = ROOT / args.results_path
    results_path.parent.mkdir(parents=True, exist_ok=True)
    output["metadata"]["results_path"] = str(results_path.relative_to(ROOT))
    results_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=float, default=100.0)
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--clients", type=int, default=100)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--mix",
        default=",".join(f"{key}={value}" for key, value in DEFAULT_MIX.items()),
    )
    parser.add_argument(
        "--scenarios",
        default="baseline,normal,sustained_backlog,burst,mixed",
    )
    parser.add_argument(
        "--evaluation-mix",
        default="",
        help="Optional light=N,medium=N,heavy=N override for pressure scenarios.",
    )
    parser.add_argument(
        "--results-path",
        default="docs/interactive-capacity-results.json",
    )
    parser.add_argument("--seed", type=int, default=20260922)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(f"wrote {ROOT / args.results_path}")
    for name, scenario in result["scenarios"].items():
        interactive = scenario["interactive"]
        print(
            name,
            f"rps={interactive['achieved_requests_per_second']}",
            f"p95={interactive['latency']['p95_ms']}ms",
            f"errors={interactive['errors']}",
        )


if __name__ == "__main__":
    main()

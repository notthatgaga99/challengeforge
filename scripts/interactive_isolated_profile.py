#!/usr/bin/env python
"""Isolated interactive-path profiler: API in a separate OS process.

Same endpoint mix and stage semantics as interactive_profile.py, but the load
generator no longer shares the API process/event loop.

Does not introduce caching or other product optimizations — measurement only.
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

import httpx
import psutil
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from challengeforge.config import Settings
from challengeforge.main import create_app
from challengeforge.worker import EvaluationWorker
from concurrency_experiment import asyncpg_url, choose_port, utc_iso, wait_for_server
from interactive_profile import (
    DEFAULT_ENDPOINTS,
    MAX_CLIENTS,
    MAX_DURATION_SECONDS,
    MAX_REQUESTS,
    drive,
    queue_position_bench,
    seed_world,
)
from resource_capacity_experiment import ResourceExperiment, env_snapshot

SAME_PROCESS_BASELINE = ROOT / "docs" / "interactive-path-profile-baseline.json"
DEFAULT_RATES = "10,20,40,60,80,100"


def parse_float_list(raw: str) -> list[float]:
    values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("rate list must not be empty")
    return values


def clients_for_rate(rate: float) -> int:
    """Match prior harness: 8 clients at ~10 RPS, then concurrency ≈ offered RPS."""
    if rate <= 10:
        return 8
    return min(MAX_CLIENTS, max(8, int(round(rate))))


def saturation_reason(interactive: dict[str, Any], offered: float) -> str | None:
    """Return a stop reason when measurements are no longer a clean capacity signal."""
    attempted = max(1, int(interactive.get("attempted", 0) or 0))
    errors = int(interactive.get("error_count", 0) or 0)
    timeouts = int(interactive.get("timeout_count", 0) or 0)
    achieved = float(interactive.get("achieved_requests_per_second", 0.0) or 0.0)
    client = interactive.get("client_latency_ms") or {}
    server = interactive.get("server_total_ms") or {}
    error_rate = errors / attempted
    timeout_rate = timeouts / attempted
    if timeout_rate >= 0.05:
        return f"timeout_rate={timeout_rate:.3f}"
    if error_rate >= 0.05:
        return f"error_rate={error_rate:.3f}"
    if offered >= 10 and achieved < 0.7 * offered:
        return f"achieved_rps={achieved:.2f} < 70% of offered={offered:.2f}"
    if float(client.get("p95_ms", 0.0) or 0.0) >= 2000:
        return f"client_p95_ms={client.get('p95_ms')}"
    if float(server.get("p95_ms", 0.0) or 0.0) >= 2000:
        return f"server_p95_ms={server.get('p95_ms')}"
    return None


def _api_process_main(
    database_url: str,
    host: str,
    port: int,
    artifact_root: str,
    ready_event: Any,
) -> None:
    """Child process entry: run uvicorn until killed."""
    import uvicorn

    settings = Settings(
        database_url=database_url,
        artifact_root=Path(artifact_root),
        log_level="WARNING",
        db_pool_size=8,
        db_max_overflow=4,
        evaluation_max_workers=1,
        request_profiling_enabled=True,
    )
    app = create_app(settings)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    ready_event.set()
    server.run()


def _worker_process_main(
    database_url: str,
    artifact_root: str,
    worker_count: int,
    stop_event: Any,
) -> None:
    """Child process: evaluation workers isolated from API and load generator."""
    import asyncio

    from challengeforge.persistence.session import create_engine as create_cf_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async def _run() -> None:
        settings = Settings(
            database_url=database_url,
            artifact_root=Path(artifact_root),
            log_level="WARNING",
            evaluation_poll_interval_seconds=0.05,
            evaluation_fake_work_ms=0,
            evaluation_stale_after_seconds=30,
            evaluation_max_workers=max(1, worker_count),
            request_profiling_enabled=False,
        )
        engine = create_cf_engine(settings)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workers = [
            EvaluationWorker(
                settings,
                worker_id=f"iso-proc-w{i}",
                session_factory=sessions,
            )
            for i in range(worker_count)
        ]
        tasks = [asyncio.create_task(worker.run_forever()) for worker in workers]
        try:
            while not stop_event.is_set():
                await asyncio.sleep(0.05)
        finally:
            for worker in workers:
                worker.request_stop()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.dispose()

    asyncio.run(_run())


def start_api_process(
    database_url: str, host: str, port: int, artifact_root: Path
) -> tuple[mp.Process, Any]:
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(
        target=_api_process_main,
        args=(database_url, host, port, str(artifact_root), ready),
        daemon=True,
        name="challengeforge-api-profile",
    )
    process.start()
    return process, ready


def start_worker_process(
    database_url: str, artifact_root: Path, worker_count: int
) -> tuple[mp.Process, Any]:
    ctx = mp.get_context("spawn")
    stop = ctx.Event()
    process = ctx.Process(
        target=_worker_process_main,
        args=(database_url, str(artifact_root), worker_count, stop),
        daemon=True,
        name="challengeforge-eval-workers",
    )
    process.start()
    return process, stop


def stop_process(process: mp.Process | None, stop_event: Any | None = None) -> None:
    if process is None:
        return
    if stop_event is not None:
        stop_event.set()
    if process.is_alive():
        process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=2)


def load_same_process_baseline(path: Path = SAME_PROCESS_BASELINE) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_for_comparison(interactive: dict[str, Any]) -> dict[str, Any]:
    return {
        "offered_requests_per_second": interactive.get("offered_requests_per_second"),
        "achieved_requests_per_second": interactive.get("achieved_requests_per_second"),
        "error_count": interactive.get("error_count"),
        "timeout_count": interactive.get("timeout_count"),
        "client_latency_ms": interactive.get("client_latency_ms"),
        "server_total_ms": interactive.get("server_total_ms"),
        "pool_wait_ms": interactive.get("pool_wait_ms"),
        "sql_ms": interactive.get("sql_ms"),
        "non_db_ms": interactive.get("non_db_ms"),
        "client_process": interactive.get("client_process"),
        "server_process": interactive.get("server_process"),
        "load_generator_note": interactive.get("load_generator_note"),
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
    server_pid: int,
    http_timeout_seconds: float,
    database_url: str,
    artifact_root: Path,
) -> dict[str, Any]:
    await experiment.prepare_database()
    world = await seed_world(experiment, client, dataset=dataset)
    worker_proc: mp.Process | None = None
    worker_stop: Any | None = None
    if workers > 0:
        worker_proc, worker_stop = start_worker_process(
            database_url, artifact_root, workers
        )
        # Brief settle so the first poll loop is live before interactive traffic.
        await asyncio.sleep(0.2)
    try:
        interactive = await drive(
            client,
            experiment,
            world,
            endpoints=endpoints,
            rate=rate,
            duration=duration,
            clients=clients,
            server_pid=server_pid,
            http_timeout_seconds=http_timeout_seconds,
            load_generator_note=(
                "Load generator is a separate OS process from the API; "
                "evaluation workers (when enabled) run in a third OS process; "
                "all still share the same laptop host/CPU/memory"
            ),
        )
    finally:
        stop_process(worker_proc, worker_stop)

    position = await queue_position_bench(
        experiment,
        world["challenge_id"],
        depths=[10, 100, 1000],
    )
    reason = saturation_reason(interactive, rate)
    return {
        "name": name,
        "dataset": {
            "dataset": world["dataset"],
            "submission_count": world["submission_count"],
            "queued_extra": world["queued_extra"],
            "challenge_id": world["challenge_id"],
        },
        "workers": workers,
        "worker_process_pid": worker_proc.pid if worker_proc is not None else None,
        "offered_requests_per_second": rate,
        "concurrent_clients": clients,
        "interactive": interactive,
        "queue_position_bench": position,
        "saturation_reason": reason,
        "saturated": reason is not None,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    rates = [min(max(r, 1.0), 120.0) for r in parse_float_list(args.rates)]
    duration = min(max(args.duration, 1.0), MAX_DURATION_SECONDS)
    endpoints = [item.strip() for item in args.endpoints.split(",") if item.strip()]
    datasets = [item.strip() for item in args.datasets.split(",") if item.strip()]
    worker_modes = [int(item) for item in args.worker_modes.split(",")]
    http_timeout = float(args.http_timeout)

    from pgserver import get_server

    data_dir = ROOT / ".pgserver-isolated-profile"
    data_dir.mkdir(exist_ok=True)
    embedded = get_server(str(data_dir))
    database_url = asyncpg_url(embedded.get_uri())
    port = choose_port()
    host = "127.0.0.1"
    base_url = f"http://{host}:{port}"
    artifact_root = ROOT / "data" / "isolated-profile-artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)

    experiment = ResourceExperiment(database_url, base_url)
    await experiment.prepare_database()

    api_proc, ready = start_api_process(database_url, host, port, artifact_root)
    # Child sets ready just before server.run(); give the loop a moment to bind.
    ready.wait(timeout=30)
    await wait_for_server(base_url)
    if not api_proc.is_alive():
        raise RuntimeError("API child process exited before load generation")

    same_process = load_same_process_baseline()
    output: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "python": sys.version.split()[0],
            "database_mode": "embedded PostgreSQL",
            "isolation": (
                "api_separate_os_process; "
                "evaluation_workers_third_process_when_enabled; "
                "load_generator_parent_process"
            ),
            "api_pid": api_proc.pid,
            "load_generator_pid": psutil.Process().pid,
            "duration_seconds": duration,
            "rates": rates,
            "endpoints": endpoints,
            "datasets": datasets,
            "worker_modes": worker_modes,
            "db_pool_size": 8,
            "db_max_overflow": 4,
            "request_profiling_enabled": True,
            "http_timeout_seconds": http_timeout,
            "safety": {
                "max_duration_seconds": MAX_DURATION_SECONDS,
                "max_requests": MAX_REQUESTS,
                "max_clients": MAX_CLIENTS,
            },
            "same_process_baseline_path": str(SAME_PROCESS_BASELINE.relative_to(ROOT))
            if SAME_PROCESS_BASELINE.exists()
            else None,
        },
        "same_process_reference": None,
        "scenarios": {},
        "rate_sweep": {},
        "stopped_early": None,
    }
    if same_process is not None:
        ref: dict[str, Any] = {"metadata": same_process.get("metadata"), "scenarios": {}}
        for name, scenario in (same_process.get("scenarios") or {}).items():
            interactive = scenario.get("interactive") or {}
            ref["scenarios"][name] = {
                "workers": scenario.get("workers"),
                "summary": summarize_for_comparison(
                    {
                        **interactive,
                        "offered_requests_per_second": same_process["metadata"].get(
                            "target_request_rate"
                        ),
                    }
                ),
            }
        output["same_process_reference"] = ref

    try:
        async with experiment.db.connect() as connection:
            output["metadata"]["postgresql_version"] = (
                await connection.execute(text("SHOW server_version"))
            ).scalar_one()

        stop_all = False
        for rate in rates:
            if stop_all:
                break
            clients = (
                min(MAX_CLIENTS, max(1, args.clients))
                if args.clients > 0
                else clients_for_rate(rate)
            )
            rate_key = f"rate_{int(rate) if rate == int(rate) else rate}"
            output["rate_sweep"][rate_key] = {}
            for dataset in datasets:
                if stop_all:
                    break
                for workers in worker_modes:
                    name = f"{dataset}_workers_{workers}_rps_{int(rate)}"
                    print(f"scenario {name}", flush=True)
                    limits = httpx.Limits(
                        max_connections=clients,
                        max_keepalive_connections=min(clients, 40),
                    )
                    timeout = httpx.Timeout(http_timeout)
                    async with httpx.AsyncClient(
                        base_url=base_url, limits=limits, timeout=timeout
                    ) as client:
                        result = await run_scenario(
                            experiment,
                            client,
                            name=name,
                            dataset=dataset,
                            endpoints=endpoints,
                            rate=rate,
                            duration=duration,
                            clients=clients,
                            workers=workers,
                            server_pid=api_proc.pid,
                            http_timeout_seconds=http_timeout,
                            database_url=database_url,
                            artifact_root=artifact_root,
                        )
                    output["scenarios"][name] = result
                    output["rate_sweep"][rate_key][f"workers_{workers}"] = {
                        "name": name,
                        "summary": summarize_for_comparison(result["interactive"]),
                        "saturated": result["saturated"],
                        "saturation_reason": result["saturation_reason"],
                    }
                    print(
                        f"  achieved={result['interactive']['achieved_requests_per_second']} "
                        f"client_p95={result['interactive']['client_latency_ms']['p95_ms']} "
                        f"server_p95={result['interactive']['server_total_ms']['p95_ms']} "
                        f"errors={result['interactive']['error_count']} "
                        f"saturated={result['saturated']}",
                        flush=True,
                    )
                    if result["saturated"] and not args.continue_on_saturation:
                        output["stopped_early"] = {
                            "at_rate": rate,
                            "scenario": name,
                            "reason": result["saturation_reason"],
                        }
                        stop_all = True
                        break
    finally:
        output["metadata"]["completed_at"] = utc_iso()
        stop_process(api_proc)
        await experiment.close()
        _ = embedded

    results_path = ROOT / args.results_path
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"wrote {results_path}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rates", default=DEFAULT_RATES)
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument(
        "--clients",
        type=int,
        default=0,
        help="Override concurrent clients; 0 means scale with rate",
    )
    parser.add_argument("--endpoints", default=DEFAULT_ENDPOINTS)
    parser.add_argument("--datasets", default="small")
    parser.add_argument("--worker-modes", default="0,1")
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument(
        "--continue-on-saturation",
        action="store_true",
        help="Do not stop the rate sweep when saturation criteria trip",
    )
    parser.add_argument(
        "--results-path",
        default="docs/interactive-isolated-load-profile-results.json",
    )
    args = parser.parse_args()
    # Windows spawn-safe entry.
    mp.freeze_support()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

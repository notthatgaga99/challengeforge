#!/usr/bin/env python
"""Closed-loop interactive feedback experiment (A/B/C).

A — resource runtime on, interactive p95 feedback DISABLED (thresholds 0)
B — feedback enabled (WARN→reduce / CRITICAL→no new HEAVY)
C — stronger (CRITICAL→hold all new evaluation starts)

Isolated: API / workers / load-gen are separate OS processes.
Does not add Redis/K8s/LLM. Product default remains feedback-off.
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
from challengeforge.evaluation.plan import EvaluationMode
from challengeforge.main import create_app
from concurrency_experiment import asyncpg_url, choose_port, utc_iso, wait_for_server
from hard_mix_pressure_experiment import (
    MAX_DRAIN_SECONDS,
    MAX_EVALS_PER_SCENARIO,
    collect_evaluation_quality,
    create_submitted_kind,
    enqueue_batch,
    enqueue_sustained,
    expand_mix,
    latency,
    mix_summary,
    wait_drain,
)
from interactive_isolated_profile import (
    clients_for_rate,
    saturation_reason,
    stop_process,
)
from interactive_profile import DEFAULT_ENDPOINTS, drive, seed_world
from resource_capacity_experiment import ResourceExperiment, env_snapshot

# Experiment thresholds (not product defaults).
WARN_MS = 300.0
CRITICAL_MS = 900.0
RECOVERY_MS = 220.0
WINDOW_S = 5.0
MIN_SAMPLES = 8


def mode_settings(mode: str) -> dict[str, Any]:
    base = {
        "resource_aware_runtime_enabled": True,
        "evaluation_min_workers": 1,
        "evaluation_max_workers": 2,
        "evaluation_max_concurrent_heavy": 1,
        "evaluation_scheduling_policy": "bounded_light_bypass",
        "request_profiling_enabled": True,
        "resource_interactive_window_seconds": WINDOW_S,
        "resource_interactive_min_samples": MIN_SAMPLES,
        "resource_interactive_sustain_seconds": 1.0,
        "resource_interactive_persist_every_samples": 3,
        "resource_adjust_cooldown_seconds": 1.0,
    }
    if mode == "A":
        base.update(
            {
                "resource_interactive_p95_warn_ms": 0.0,
                "resource_interactive_p95_critical_ms": 0.0,
                "resource_interactive_critical_hold_all": False,
            }
        )
    elif mode == "B":
        base.update(
            {
                "resource_interactive_p95_warn_ms": WARN_MS,
                "resource_interactive_p95_critical_ms": CRITICAL_MS,
                "resource_interactive_p95_recovery_ms": RECOVERY_MS,
                "resource_interactive_critical_hold_all": False,
            }
        )
    elif mode == "C":
        base.update(
            {
                "resource_interactive_p95_warn_ms": WARN_MS,
                "resource_interactive_p95_critical_ms": CRITICAL_MS,
                "resource_interactive_p95_recovery_ms": RECOVERY_MS,
                "resource_interactive_critical_hold_all": True,
            }
        )
    else:
        raise ValueError(mode)
    return base


def _api_main(
    database_url: str,
    host: str,
    port: int,
    artifact_root: str,
    ready_event: Any,
    settings_overrides: dict[str, Any],
) -> None:
    import uvicorn

    kwargs = {
        "database_url": database_url,
        "artifact_root": Path(artifact_root),
        "log_level": "WARNING",
        "db_pool_size": 8,
        "db_max_overflow": 4,
        "evaluation_max_workers": 1,
        "request_profiling_enabled": True,
    }
    kwargs.update(settings_overrides)
    settings = Settings(**kwargs)
    app = create_app(settings)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    ready_event.set()
    server.run()


def _worker_main(
    database_url: str,
    artifact_root: str,
    worker_count: int,
    stop_event: Any,
    settings_overrides: dict[str, Any],
) -> None:
    import asyncio

    from challengeforge.persistence.session import create_engine as create_cf_engine
    from challengeforge.worker import EvaluationWorker
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async def _run() -> None:
        base = {
            "database_url": database_url,
            "artifact_root": Path(artifact_root),
            "log_level": "WARNING",
            "evaluation_poll_interval_seconds": 0.05,
            "evaluation_fake_work_ms": 0,
            "evaluation_stale_after_seconds": 30,
            "evaluation_max_workers": max(1, worker_count),
            "evaluation_min_workers": 1,
            "request_profiling_enabled": False,
            "resource_aware_runtime_enabled": True,
            "evaluation_scheduling_policy": "bounded_light_bypass",
            "evaluation_progressive_mode": "legacy",
        }
        base.update(settings_overrides)
        settings = Settings(**base)
        engine = create_cf_engine(settings)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workers = [
            EvaluationWorker(
                settings, worker_id=f"ifb-w{i}", session_factory=sessions
            )
            for i in range(max(1, worker_count))
        ]
        tasks = [asyncio.create_task(w.run_forever()) for w in workers]
        try:
            while not stop_event.is_set():
                await asyncio.sleep(0.05)
        finally:
            for w in workers:
                w.request_stop()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.dispose()

    asyncio.run(_run())


def start_api(
    database_url: str,
    host: str,
    port: int,
    artifact_root: Path,
    overrides: dict[str, Any],
) -> tuple[mp.Process, Any]:
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(
        target=_api_main,
        args=(database_url, host, port, str(artifact_root), ready, overrides),
        daemon=True,
        name="cf-interactive-feedback-api",
    )
    proc.start()
    return proc, ready


def start_workers(
    database_url: str,
    artifact_root: Path,
    worker_count: int,
    overrides: dict[str, Any],
) -> tuple[mp.Process, Any]:
    ctx = mp.get_context("spawn")
    stop = ctx.Event()
    proc = ctx.Process(
        target=_worker_main,
        args=(database_url, str(artifact_root), worker_count, stop, overrides),
        daemon=True,
        name="cf-interactive-feedback-workers",
    )
    proc.start()
    return proc, stop


async def read_controller_state(experiment: ResourceExperiment) -> dict[str, Any]:
    async with experiment.worker_sessions() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT pressure_state, adaptive_max_workers,
                           interactive_p95_ms, interactive_sample_count
                    FROM evaluation_scheduler_state WHERE id = 1
                    """
                )
            )
        ).mappings().first()
        await session.rollback()
    return dict(row) if row else {}


async def run_scenario(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    *,
    name: str,
    mode: str,
    mix_name: str,
    pressure_level: str,
    interactive_rps: float,
    duration: float,
    eval_count: int,
    eval_arrival_rps: float,
    clients: int,
    workers: int,
    server_pid: int,
    database_url: str,
    artifact_root: Path,
    settings_overrides: dict[str, Any],
    seed: int,
    http_timeout: float,
) -> dict[str, Any]:
    await experiment.prepare_database()
    world = await seed_world(experiment, client, dataset="small")
    _, pressure_id = await experiment.create_published_challenge(
        client, f"ifb-{name[:40]}"
    )
    kinds = expand_mix(mix_name, total=eval_count, seed=seed)
    evaluation_mode = EvaluationMode.FIXED_PROGRESSIVE.value

    pre_kinds: list[str] = []
    sustained_kinds: list[str] = []
    if pressure_level == "low":
        pre_kinds = kinds[: max(1, len(kinds) // 4)]
    elif pressure_level == "medium":
        sustained_kinds = kinds
    elif pressure_level in ("high", "burst", "abusive"):
        pre_kinds = kinds
    else:
        raise ValueError(pressure_level)

    accept_latencies: list[float] = []
    submission_ids: list[str] = []
    if pre_kinds:
        ids, accepts = await enqueue_batch(
            experiment,
            client,
            pressure_id,
            pre_kinds,
            evaluation_mode=evaluation_mode,
            index_base=seed * 1000,
        )
        submission_ids.extend(ids)
        accept_latencies.extend(accepts)

    worker_proc, worker_stop = start_workers(
        database_url, artifact_root, workers, settings_overrides
    )
    await asyncio.sleep(0.2)

    api_proc = psutil.Process(server_pid)
    lg_proc = psutil.Process()
    api_proc.cpu_percent(None)
    lg_proc.cpu_percent(None)
    worker_ps = psutil.Process(worker_proc.pid) if worker_proc.is_alive() else None
    if worker_ps:
        worker_ps.cpu_percent(None)

    controller_samples: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    first_warn_at: float | None = None
    first_critical_at: float | None = None
    first_p95_cross_warn: float | None = None

    async def sample_controller() -> None:
        nonlocal first_warn_at, first_critical_at, first_p95_cross_warn
        while time.perf_counter() - t0 < duration + MAX_DRAIN_SECONDS:
            st = await read_controller_state(experiment)
            st["t"] = round(time.perf_counter() - t0, 3)
            controller_samples.append(st)
            p95 = st.get("interactive_p95_ms")
            if (
                p95 is not None
                and p95 >= WARN_MS
                and first_p95_cross_warn is None
            ):
                first_p95_cross_warn = st["t"]
            pressure = st.get("pressure_state")
            if pressure == "pressured" and first_warn_at is None:
                first_warn_at = st["t"]
            if pressure == "degraded" and first_critical_at is None:
                first_critical_at = st["t"]
            await asyncio.sleep(0.25)

    sampler = asyncio.create_task(sample_controller())

    sustained_task = None
    if sustained_kinds:
        sustained_task = asyncio.create_task(
            enqueue_sustained(
                experiment,
                client,
                pressure_id,
                sustained_kinds,
                evaluation_mode=evaluation_mode,
                index_base=seed * 1000 + 500,
                rate=eval_arrival_rps,
                duration=duration,
            )
        )

    interactive = await drive(
        client,
        experiment,
        world,
        endpoints=[e.strip() for e in DEFAULT_ENDPOINTS.split(",")],
        rate=interactive_rps,
        duration=duration,
        clients=clients,
        server_pid=server_pid,
        http_timeout_seconds=http_timeout,
        load_generator_note=(
            "interactive-feedback: API/workers/load-gen separate OS processes"
        ),
    )

    if sustained_task is not None:
        ids, accepts = await sustained_task
        submission_ids.extend(ids)
        accept_latencies.extend(accepts)

    drain = await wait_drain(
        experiment, challenge_id=pressure_id, timeout_seconds=MAX_DRAIN_SECONDS
    )
    sampler.cancel()
    try:
        await sampler
    except asyncio.CancelledError:
        pass

    def _safe_cpu(proc: psutil.Process | None) -> float | None:
        if proc is None:
            return None
        try:
            if not proc.is_running():
                return None
            return proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def _safe_rss(proc: psutil.Process | None) -> float | None:
        if proc is None:
            return None
        try:
            if not proc.is_running():
                return None
            return proc.memory_info().rss / (1024 * 1024)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    api_cpu = _safe_cpu(api_proc)
    lg_cpu = _safe_cpu(lg_proc)
    worker_cpu = _safe_cpu(worker_ps)
    api_rss = _safe_rss(api_proc)
    lg_rss = _safe_rss(lg_proc)

    stop_process(worker_proc, worker_stop)
    quality = await collect_evaluation_quality(experiment, pressure_id)

    async with experiment.db.connect() as conn:
        db_conn = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database()"
                )
            )
        ).scalar_one()

    sat = saturation_reason(interactive, interactive_rps)
    wall = max(0.001, time.perf_counter() - t0)
    pressures = sorted(
        {s.get("pressure_state") for s in controller_samples if s.get("pressure_state")}
    )
    p95_obs = [
        float(s["interactive_p95_ms"])
        for s in controller_samples
        if s.get("interactive_p95_ms") is not None
    ]
    reaction = None
    if first_p95_cross_warn is not None and first_warn_at is not None:
        reaction = round(first_warn_at - first_p95_cross_warn, 3)
    elif first_p95_cross_warn is not None and first_critical_at is not None:
        reaction = round(first_critical_at - first_p95_cross_warn, 3)

    return {
        "name": name,
        "mode": mode,
        "mix": mix_name,
        "pressure_level": pressure_level,
        "mix_summary": mix_summary(kinds),
        "interactive": {
            "offered_rps": interactive_rps,
            "duration_seconds": duration,
            "clients": clients,
            **interactive,
        },
        "saturation_reason": sat,
        "saturated": sat is not None,
        "evaluation": {
            "arrived": len(submission_ids),
            "accept_latency_ms": latency(accept_latencies),
            "completion_rate_per_second": round(
                int(quality["completed"]) / wall, 3
            ),
            **quality,
        },
        "drain": {
            "max_queue_depth": (drain or {}).get("max_queue_depth"),
            "ending_queued": (drain or {}).get("ending_queued"),
            "pressure_states_seen": (drain or {}).get("pressure_states_seen"),
        },
        "controller": {
            "pressures_seen": pressures,
            "samples": len(controller_samples),
            "interactive_p95_observed": latency(p95_obs) if p95_obs else None,
            "first_p95_cross_warn_s": first_p95_cross_warn,
            "first_pressured_s": first_warn_at,
            "first_degraded_s": first_critical_at,
            "reaction_latency_s": reaction,
            "adaptive_max_seen": [
                s.get("adaptive_max_workers") for s in controller_samples
            ],
        },
        "resources": {
            "api_cpu_percent": api_cpu,
            "worker_cpu_percent": worker_cpu,
            "load_generator_cpu_percent": lg_cpu,
            "api_rss_mb": round(api_rss, 2) if api_rss is not None else None,
            "load_generator_rss_mb": round(lg_rss, 2) if lg_rss is not None else None,
            "db_connections": int(db_conn),
            "workers_configured": workers,
        },
        "settings": settings_overrides,
        "seed": seed,
    }


def portfolio_plan() -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for mode in ("A", "B", "C"):
        for mix, pressure, n, arrival in (
            ("mostly_easy", "medium", 10, 2.0),
            ("hard_70", "medium", 12, 2.0),
            ("hard_90", "medium", 12, 2.0),
            ("adversarial", "medium", 12, 2.0),
            ("heavy_dominated", "medium", 12, 2.0),
            ("adversarial", "abusive", 20, 3.0),
            ("hard_70", "burst", 16, 2.0),
        ):
            scenarios.append(
                {
                    "mode": mode,
                    "mix": mix,
                    "pressure": pressure,
                    "rps": 20.0,
                    "eval_count": min(n, MAX_EVALS_PER_SCENARIO),
                    "eval_arrival_rps": arrival,
                    "tag": "abc_matrix",
                }
            )
    # Threshold / adversarial control focused cell
    scenarios.append(
        {
            "mode": "B",
            "mix": "adversarial",
            "pressure": "abusive",
            "rps": 20.0,
            "eval_count": 20,
            "eval_arrival_rps": 3.0,
            "tag": "adversarial_control",
        }
    )
    return scenarios


def smoke_plan() -> list[dict[str, Any]]:
    return [
        {
            "mode": "B",
            "mix": "hard_70",
            "pressure": "medium",
            "rps": 10.0,
            "eval_count": 8,
            "eval_arrival_rps": 1.5,
            "tag": "smoke",
        }
    ]


def derive_verdict(scenarios: dict[str, Any]) -> dict[str, Any]:
    by_mode: dict[str, list] = {"A": [], "B": [], "C": []}
    for sc in scenarios.values():
        by_mode.setdefault(sc["mode"], []).append(sc)

    def stats(mode: str) -> dict[str, Any]:
        rows = by_mode.get(mode) or []
        if not rows:
            return {}
        p95s = [
            (r["interactive"].get("client_latency_ms") or {}).get("p95_ms")
            for r in rows
        ]
        p95s = [p for p in p95s if p is not None]
        agrees = [
            (r["evaluation"].get("quality") or {}).get("decision_agreement")
            for r in rows
        ]
        agrees = [a for a in agrees if a is not None]
        saturated = sum(1 for r in rows if r.get("saturated"))
        stranded = sum(int(r["evaluation"].get("stranded") or 0) for r in rows)
        fep = sum(
            int((r["evaluation"].get("quality") or {}).get("false_early_pass") or 0)
            for r in rows
        )
        completed = sum(int(r["evaluation"].get("completed") or 0) for r in rows)
        return {
            "n": len(rows),
            "mean_interactive_p95_ms": round(sum(p95s) / len(p95s), 2) if p95s else None,
            "max_interactive_p95_ms": max(p95s) if p95s else None,
            "saturated_cells": saturated,
            "mean_decision_agreement": (
                round(sum(agrees) / len(agrees), 4) if agrees else None
            ),
            "false_early_pass": fep,
            "stranded": stranded,
            "evaluations_completed": completed,
        }

    a, b, c = stats("A"), stats("B"), stats("C")
    improved = None
    if a.get("mean_interactive_p95_ms") and b.get("mean_interactive_p95_ms"):
        improved = round(
            a["mean_interactive_p95_ms"] - b["mean_interactive_p95_ms"], 2
        )
    return {
        "A": a,
        "B": b,
        "C": c,
        "p95_improvement_A_minus_B_ms": improved,
        "correctness_ok": all(
            (s.get("mean_decision_agreement") in (None, 1.0))
            and s.get("false_early_pass", 0) == 0
            and s.get("stranded", 0) == 0
            for s in (a, b, c)
            if s
        ),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from pgserver import get_server

    plan = smoke_plan() if args.profile == "smoke" else portfolio_plan()
    data_dir = ROOT / ".pgserver-interactive-feedback"
    data_dir.mkdir(exist_ok=True)
    embedded = get_server(str(data_dir))
    database_url = asyncpg_url(embedded.get_uri())
    port = choose_port()
    host = "127.0.0.1"
    base_url = f"http://{host}:{port}"
    artifact_root = ROOT / "data" / "interactive-feedback-artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)

    # Start with mode A API settings; recreate per mode group.
    output: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "profile": args.profile,
            "thresholds": {
                "warn_ms": WARN_MS,
                "critical_ms": CRITICAL_MS,
                "recovery_ms": RECOVERY_MS,
                "window_s": WINDOW_S,
                "min_samples": MIN_SAMPLES,
            },
            "signal": (
                "server wall-time for designated interactive HTTP paths; "
                "not client RTT; not evaluation queue wait"
            ),
            "claim": "Closed-loop interactive protection validation — not capacity marketing",
        },
        "scenarios": {},
        "verdict_inputs": {},
    }

    duration = min(max(args.duration, 2.0), 12.0)
    workers = max(1, args.workers)
    http_timeout = float(args.http_timeout)
    seed = 77

    # Group by mode so we restart API with correct settings.
    by_mode: dict[str, list] = {}
    for item in plan:
        by_mode.setdefault(item["mode"], []).append(item)

    experiment = ResourceExperiment(database_url, base_url)
    await experiment.prepare_database()

    try:
        for mode, items in by_mode.items():
            overrides = mode_settings(mode)
            api_proc, ready = start_api(
                database_url, host, port, artifact_root, overrides
            )
            ready.wait(timeout=30)
            await wait_for_server(base_url)
            if not api_proc.is_alive():
                raise RuntimeError(f"API exited for mode {mode}")
            try:
                for i, item in enumerate(items):
                    rps = float(item["rps"])
                    clients = (
                        min(100, max(1, args.clients))
                        if args.clients > 0
                        else clients_for_rate(rps)
                    )
                    name = (
                        f"{item['tag']}__mode{mode}__{item['mix']}__"
                        f"p{item['pressure']}__rps{int(rps)}"
                    )
                    print(f"scenario {name}", flush=True)
                    limits = httpx.Limits(
                        max_connections=max(clients, 16),
                        max_keepalive_connections=min(max(clients, 16), 40),
                    )
                    async with httpx.AsyncClient(
                        base_url=base_url,
                        limits=limits,
                        timeout=httpx.Timeout(http_timeout),
                    ) as client:
                        result = await run_scenario(
                            experiment,
                            client,
                            name=name,
                            mode=mode,
                            mix_name=item["mix"],
                            pressure_level=item["pressure"],
                            interactive_rps=rps,
                            duration=duration,
                            eval_count=item["eval_count"],
                            eval_arrival_rps=float(item["eval_arrival_rps"]),
                            clients=clients,
                            workers=workers,
                            server_pid=api_proc.pid,
                            database_url=database_url,
                            artifact_root=artifact_root,
                            settings_overrides=overrides,
                            seed=seed + i + ord(mode) * 100,
                            http_timeout=http_timeout,
                        )
                    result["tag"] = item["tag"]
                    output["scenarios"][name] = result
                    q = result["evaluation"]["quality"]
                    print(
                        f"  mode={mode} achieved={result['interactive'].get('achieved_requests_per_second')} "
                        f"p95={result['interactive'].get('client_latency_ms', {}).get('p95_ms')} "
                        f"sat={result['saturated']} agree={q.get('decision_agreement')} "
                        f"pressures={result['controller']['pressures_seen']}",
                        flush=True,
                    )
            finally:
                stop_process(api_proc)
                # Recreate experiment client target for next mode on a fresh port.
                port = choose_port()
                base_url = f"http://{host}:{port}"
                experiment = ResourceExperiment(database_url, base_url)
    finally:
        output["metadata"]["completed_at"] = utc_iso()
        await experiment.close()
        _ = embedded

    output["verdict_inputs"] = derive_verdict(output["scenarios"])
    path = ROOT / args.results_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("smoke", "portfolio"), default="portfolio")
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--clients", type=int, default=0)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument(
        "--results-path", default="docs/interactive-feedback-results.json"
    )
    args = parser.parse_args()
    mp.freeze_support()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Interactive feedback hot-path experiment (async publish + signal correlation).

Phases covered by this harness:

  hotpath   — A sync vs B async publication; feedback DISABLED (thresholds 0)
  missed    — reproduce client≫server p95 cell with async publish
  correlate — time-series of server wall / pool wait / in-flight vs client p95
  control   — A off / B server-p95 feedback with async publish (optional C)

Does not add Redis. Product defaults remain feedback-off + async publish.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import statistics
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
    collect_evaluation_quality,
    enqueue_batch,
    enqueue_sustained,
    expand_mix,
    latency,
    mix_summary,
    wait_drain,
)
from interactive_feedback_experiment import (
    CRITICAL_MS,
    MIN_SAMPLES,
    RECOVERY_MS,
    WARN_MS,
    WINDOW_S,
    start_api,
    start_workers,
)
from interactive_isolated_profile import clients_for_rate, saturation_reason, stop_process
from interactive_profile import DEFAULT_ENDPOINTS, drive, seed_world
from resource_capacity_experiment import ResourceExperiment, env_snapshot


def publish_overrides(
    *,
    publish_mode: str,
    feedback: bool = False,
    hold_all: bool = False,
    interval_ms: float = 250.0,
) -> dict[str, Any]:
    base: dict[str, Any] = {
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
        "resource_interactive_publish_mode": publish_mode,
        "resource_interactive_publish_interval_ms": interval_ms,
        "resource_interactive_publish_min_delta_ms": 25.0,
        "resource_adjust_cooldown_seconds": 1.0,
        "resource_interactive_critical_hold_all": hold_all,
    }
    if feedback:
        base.update(
            {
                "resource_interactive_p95_warn_ms": WARN_MS,
                "resource_interactive_p95_critical_ms": CRITICAL_MS,
                "resource_interactive_p95_recovery_ms": RECOVERY_MS,
            }
        )
    else:
        base.update(
            {
                "resource_interactive_p95_warn_ms": 0.0,
                "resource_interactive_p95_critical_ms": 0.0,
            }
        )
    return base


async def read_hint(experiment: ResourceExperiment) -> dict[str, Any]:
    async with experiment.worker_sessions() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT pressure_state, adaptive_max_workers,
                           interactive_p95_ms, interactive_sample_count,
                           pool_wait_p95_ms
                    FROM evaluation_scheduler_state WHERE id = 1
                    """
                )
            )
        ).mappings().first()
        await session.rollback()
    return dict(row) if row else {}


async def read_local_metrics(client: httpx.AsyncClient) -> dict[str, Any]:
    try:
        r = await client.get("/debug/interactive-metrics", timeout=2.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"available": False}


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


async def run_cell(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    *,
    name: str,
    tag: str,
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
    sample_interval: float = 0.25,
    collect_series: bool = True,
) -> dict[str, Any]:
    await experiment.prepare_database()
    world = await seed_world(experiment, client, dataset="small")
    _, pressure_id = await experiment.create_published_challenge(
        client, f"hotpath-{name[:40]}"
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

    series: list[dict[str, Any]] = []
    controller_samples: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    first_warn_at: float | None = None
    first_critical_at: float | None = None
    first_p95_cross_warn: float | None = None
    client_cross_warn_at: float | None = None

    async def sampler() -> None:
        nonlocal first_warn_at, first_critical_at, first_p95_cross_warn
        while time.perf_counter() - t0 < duration + MAX_DRAIN_SECONDS:
            t = round(time.perf_counter() - t0, 3)
            st = await read_hint(experiment)
            st["t"] = t
            controller_samples.append(st)
            local = await read_local_metrics(client) if collect_series else {}
            if collect_series:
                series.append(
                    {
                        "t": t,
                        "server_p95_ms": st.get("interactive_p95_ms"),
                        "pool_wait_p95_ms": st.get("pool_wait_p95_ms"),
                        "sample_count": st.get("interactive_sample_count"),
                        "pressure_state": st.get("pressure_state"),
                        "adaptive_max_workers": st.get("adaptive_max_workers"),
                        "in_flight": local.get("in_flight"),
                        "peak_in_flight": local.get("peak_in_flight"),
                        "local_p95_ms": local.get("p95_ms"),
                        "local_pool_wait_p95_ms": local.get("pool_wait_p95_ms"),
                        "publish_writes": local.get("publish_writes"),
                        "publish_failures": local.get("publish_failures"),
                        "api_cpu": _safe_cpu(api_proc),
                        "worker_cpu": _safe_cpu(worker_ps),
                        "db_connections": None,
                    }
                )
            p95 = st.get("interactive_p95_ms")
            if p95 is not None and p95 >= WARN_MS and first_p95_cross_warn is None:
                first_p95_cross_warn = t
            pressure = st.get("pressure_state")
            if pressure == "pressured" and first_warn_at is None:
                first_warn_at = t
            if pressure == "degraded" and first_critical_at is None:
                first_critical_at = t
            await asyncio.sleep(sample_interval)

    sample_task = asyncio.create_task(sampler())

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
        load_generator_note="hotpath: API/workers/load-gen separate OS processes",
    )

    if sustained_task is not None:
        ids, accepts = await sustained_task
        submission_ids.extend(ids)
        accept_latencies.extend(accepts)

    drain = await wait_drain(
        experiment, challenge_id=pressure_id, timeout_seconds=MAX_DRAIN_SECONDS
    )
    sample_task.cancel()
    try:
        await sample_task
    except asyncio.CancelledError:
        pass

    # Final metrics snapshot
    final_local = await read_local_metrics(client)
    final_hint = await read_hint(experiment)

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
    client_p95 = (interactive.get("client_latency_ms") or {}).get("p95_ms")
    server_p95 = (interactive.get("server_total_ms") or {}).get("p95_ms")
    pool = interactive.get("pool_wait_ms") or {}

    # Qualitative: did client exceed warn while published server stayed below?
    published_p95s = [
        float(s["interactive_p95_ms"])
        for s in controller_samples
        if s.get("interactive_p95_ms") is not None
    ]
    missed = bool(
        client_p95 is not None
        and client_p95 >= WARN_MS
        and published_p95s
        and max(published_p95s) < WARN_MS
    )

    reaction = None
    if first_p95_cross_warn is not None and first_warn_at is not None:
        reaction = round(first_warn_at - first_p95_cross_warn, 3)
    elif first_p95_cross_warn is not None and first_critical_at is not None:
        reaction = round(first_critical_at - first_p95_cross_warn, 3)

    return {
        "name": name,
        "tag": tag,
        "mix": mix_name,
        "pressure_level": pressure_level,
        "mix_summary": mix_summary(kinds),
        "publish_mode": settings_overrides.get("resource_interactive_publish_mode"),
        "feedback_enabled": float(
            settings_overrides.get("resource_interactive_p95_warn_ms") or 0
        )
        > 0,
        "interactive": {
            "offered_rps": interactive_rps,
            "duration_seconds": duration,
            "clients": clients,
            **interactive,
        },
        "saturation_reason": sat,
        "saturated": sat is not None,
        "missed_intervention_pattern": missed,
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
            "pressures_seen": sorted(
                {
                    s.get("pressure_state")
                    for s in controller_samples
                    if s.get("pressure_state")
                }
            ),
            "samples": len(controller_samples),
            "published_p95": latency(published_p95s) if published_p95s else None,
            "final_hint": final_hint,
            "first_p95_cross_warn_s": first_p95_cross_warn,
            "first_pressured_s": first_warn_at,
            "first_degraded_s": first_critical_at,
            "reaction_latency_s": reaction,
        },
        "publisher": final_local,
        "signals": {
            "client_p95_ms": client_p95,
            "server_wall_p95_ms": server_p95,
            "pool_wait_p50_ms": pool.get("p50_ms"),
            "pool_wait_p95_ms": pool.get("p95_ms"),
            "pool_wait_p99_ms": pool.get("p99_ms"),
            "pool_wait_max_ms": pool.get("max_ms"),
        },
        "series": series if collect_series else [],
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


def correlate_series(series: list[dict[str, Any]], client_p95: float | None) -> dict[str, Any]:
    """Qualitative correlation helpers — no fancy model."""
    if not series:
        return {"note": "empty series"}

    def col(key: str) -> list[float]:
        out: list[float] = []
        for row in series:
            v = row.get(key)
            if v is not None:
                out.append(float(v))
        return out

    server = col("server_p95_ms")
    pool = col("pool_wait_p95_ms")
    local = col("local_p95_ms")
    inflight = col("in_flight")

    def summarize(vals: list[float]) -> dict[str, Any] | None:
        if not vals:
            return None
        return {
            "n": len(vals),
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
            "mean": round(statistics.fmean(vals), 3),
            "stdev": round(statistics.pstdev(vals), 3) if len(vals) > 1 else 0.0,
        }

    # First movement: earliest sample where signal exceeds 1.5x its early median
    def first_spike(key: str, factor: float = 1.5) -> float | None:
        vals = [(r["t"], r.get(key)) for r in series if r.get(key) is not None]
        if len(vals) < 4:
            return None
        early = [float(v) for _, v in vals[: max(2, len(vals) // 4)]]
        med = statistics.median(early) if early else 0.0
        thresh = max(med * factor, med + 10.0)
        for t, v in vals:
            if float(v) >= thresh:
                return float(t)
        return None

    client_saturated = bool(client_p95 is not None and client_p95 >= WARN_MS)
    server_max = max(server) if server else None
    pool_max = max(pool) if pool else None

    return {
        "client_p95_ms": client_p95,
        "client_exceeded_warn": client_saturated,
        "server_wall": summarize(server),
        "pool_wait": summarize(pool),
        "local_wall": summarize(local),
        "in_flight": summarize(inflight),
        "first_spike_s": {
            "server_p95": first_spike("server_p95_ms"),
            "pool_wait_p95": first_spike("pool_wait_p95_ms"),
            "local_p95": first_spike("local_p95_ms"),
            "in_flight": first_spike("in_flight"),
        },
        "missed_by_server_wall": bool(
            client_saturated and server_max is not None and server_max < WARN_MS
        ),
        "pool_wait_also_below_warn": bool(
            client_saturated and pool_max is not None and pool_max < WARN_MS
        ),
        "note": (
            "Qualitative summaries only; series length may be too small "
            "for formal correlation coefficients."
        ),
    }


def plan_for(profile: str) -> list[dict[str, Any]]:
    if profile == "smoke":
        return [
            {
                "tag": "hotpath",
                "publish_mode": "sync",
                "feedback": False,
                "mix": "hard_70",
                "pressure": "medium",
                "rps": 15.0,
                "eval_count": 8,
                "eval_arrival_rps": 1.5,
            },
            {
                "tag": "hotpath",
                "publish_mode": "async",
                "feedback": False,
                "mix": "hard_70",
                "pressure": "medium",
                "rps": 15.0,
                "eval_count": 8,
                "eval_arrival_rps": 1.5,
            },
        ]
    if profile == "hotpath":
        cells = []
        for mode in ("sync", "async"):
            for mix, pressure, n, arrival, rps in (
                ("mostly_easy", "medium", 10, 2.0, 20.0),
                ("hard_70", "medium", 12, 2.0, 20.0),
                ("adversarial", "medium", 12, 2.0, 20.0),
                ("adversarial", "abusive", 20, 3.0, 20.0),
            ):
                cells.append(
                    {
                        "tag": "hotpath",
                        "publish_mode": mode,
                        "feedback": False,
                        "mix": mix,
                        "pressure": pressure,
                        "rps": rps,
                        "eval_count": n,
                        "eval_arrival_rps": arrival,
                    }
                )
        return cells
    if profile == "missed":
        # Prior failure: adversarial medium under feedback — client sat, server ~130
        return [
            {
                "tag": "missed",
                "publish_mode": "async",
                "feedback": True,
                "mix": "adversarial",
                "pressure": "medium",
                "rps": 20.0,
                "eval_count": 12,
                "eval_arrival_rps": 2.0,
            },
            {
                "tag": "missed",
                "publish_mode": "sync",
                "feedback": True,
                "mix": "adversarial",
                "pressure": "medium",
                "rps": 20.0,
                "eval_count": 12,
                "eval_arrival_rps": 2.0,
            },
        ]
    if profile == "correlate":
        return [
            {
                "tag": "correlate",
                "publish_mode": "async",
                "feedback": False,
                "mix": "adversarial",
                "pressure": "medium",
                "rps": 20.0,
                "eval_count": 12,
                "eval_arrival_rps": 2.0,
            },
            {
                "tag": "correlate",
                "publish_mode": "async",
                "feedback": False,
                "mix": "hard_90",
                "pressure": "medium",
                "rps": 20.0,
                "eval_count": 12,
                "eval_arrival_rps": 2.0,
            },
            {
                "tag": "correlate",
                "publish_mode": "async",
                "feedback": False,
                "mix": "adversarial",
                "pressure": "abusive",
                "rps": 20.0,
                "eval_count": 20,
                "eval_arrival_rps": 3.0,
            },
        ]
    if profile == "control":
        cells = []
        for feedback, hold, label in (
            (False, False, "A"),
            (True, False, "B"),
            (True, True, "C"),
        ):
            for mix, pressure, n, arrival in (
                ("hard_70", "medium", 12, 2.0),
                ("hard_90", "medium", 12, 2.0),
                ("adversarial", "medium", 12, 2.0),
                ("adversarial", "abusive", 20, 3.0),
                ("hard_70", "burst", 16, 2.0),
            ):
                cells.append(
                    {
                        "tag": f"control_{label}",
                        "publish_mode": "async",
                        "feedback": feedback,
                        "hold_all": hold,
                        "mix": mix,
                        "pressure": pressure,
                        "rps": 20.0,
                        "eval_count": n,
                        "eval_arrival_rps": arrival,
                        "mode_label": label,
                    }
                )
        return cells
    if profile == "full":
        return (
            plan_for("hotpath")
            + plan_for("missed")
            + plan_for("correlate")
            + plan_for("control")
        )
    raise ValueError(profile)


def derive_hotpath_verdict(scenarios: dict[str, Any]) -> dict[str, Any]:
    sync_rows = [
        s for s in scenarios.values() if s.get("tag") == "hotpath" and s.get("publish_mode") == "sync"
    ]
    async_rows = [
        s
        for s in scenarios.values()
        if s.get("tag") == "hotpath" and s.get("publish_mode") == "async"
    ]

    def mean_p95(rows: list) -> float | None:
        vals = [
            (r["interactive"].get("client_latency_ms") or {}).get("p95_ms")
            for r in rows
        ]
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def mean_writes(rows: list) -> float | None:
        vals = []
        for r in rows:
            w = (r.get("publisher") or {}).get("publish_writes")
            if w is not None:
                vals.append(float(w))
        return round(sum(vals) / len(vals), 2) if vals else None

    sync_p95 = mean_p95(sync_rows)
    async_p95 = mean_p95(async_rows)
    delta = None
    if sync_p95 is not None and async_p95 is not None:
        delta = round(sync_p95 - async_p95, 2)

    missed_rows = [s for s in scenarios.values() if s.get("tag") == "missed"]
    corr_rows = [s for s in scenarios.values() if s.get("tag") == "correlate"]

    return {
        "hotpath": {
            "sync_n": len(sync_rows),
            "async_n": len(async_rows),
            "sync_mean_client_p95_ms": sync_p95,
            "async_mean_client_p95_ms": async_p95,
            "sync_minus_async_client_p95_ms": delta,
            "sync_mean_publish_writes": mean_writes(sync_rows),
            "async_mean_publish_writes": mean_writes(async_rows),
            "measurable_hotpath_gain": bool(delta is not None and delta >= 5.0),
        },
        "missed_intervention_reproduced": any(
            r.get("missed_intervention_pattern") for r in missed_rows
        ),
        "correlation_summaries": [
            {
                "name": r["name"],
                **(r.get("correlation") or {}),
            }
            for r in corr_rows
        ],
        "correctness_ok": all(
            (s["evaluation"].get("quality") or {}).get("decision_agreement") in (None, 1.0)
            and int((s["evaluation"].get("quality") or {}).get("false_early_pass") or 0)
            == 0
            and int(s["evaluation"].get("stranded") or 0) == 0
            for s in scenarios.values()
        ),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from pgserver import get_server

    plan = plan_for(args.profile)
    data_dir = ROOT / ".pgserver-interactive-hotpath"
    data_dir.mkdir(exist_ok=True)
    embedded = get_server(str(data_dir))
    database_url = asyncpg_url(embedded.get_uri())
    port = choose_port()
    host = "127.0.0.1"
    base_url = f"http://{host}:{port}"
    artifact_root = ROOT / "data" / "interactive-hotpath-artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)

    output: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "profile": args.profile,
            "publish_interval_ms": args.publish_interval_ms,
            "claim": (
                "Hot-path publish cost + signal correlation — not capacity marketing"
            ),
            "stale_signal_semantics": (
                "On publish failure, last successful hint remains in Postgres; "
                "workers keep last read value; missing samples do not force HEALTHY."
            ),
        },
        "scenarios": {},
        "verdict_inputs": {},
    }

    duration = min(max(args.duration, 2.0), 12.0)
    workers = max(1, args.workers)
    http_timeout = float(args.http_timeout)
    seed = 91

    # Group by (publish_mode, feedback, hold_all) so API restarts once per group.
    groups: dict[tuple, list] = {}
    for item in plan:
        key = (
            item["publish_mode"],
            bool(item.get("feedback")),
            bool(item.get("hold_all", False)),
        )
        groups.setdefault(key, []).append(item)

    experiment = ResourceExperiment(database_url, base_url)
    await experiment.prepare_database()

    try:
        for (pub_mode, feedback, hold_all), items in groups.items():
            overrides = publish_overrides(
                publish_mode=pub_mode,
                feedback=feedback,
                hold_all=hold_all,
                interval_ms=float(args.publish_interval_ms),
            )
            api_proc, ready = start_api(
                database_url, host, port, artifact_root, overrides
            )
            ready.wait(timeout=30)
            await wait_for_server(base_url)
            if not api_proc.is_alive():
                raise RuntimeError(f"API exited for {pub_mode}/{feedback}")
            try:
                for i, item in enumerate(items):
                    rps = float(item["rps"])
                    clients = (
                        min(100, max(1, args.clients))
                        if args.clients > 0
                        else clients_for_rate(rps)
                    )
                    label = item.get("mode_label") or (
                        "fb" if feedback else "off"
                    )
                    name = (
                        f"{item['tag']}__{pub_mode}__{label}__{item['mix']}__"
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
                        result = await run_cell(
                            experiment,
                            client,
                            name=name,
                            tag=item["tag"],
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
                            seed=seed + i * 17 + hash(pub_mode) % 1000,
                            http_timeout=http_timeout,
                            collect_series=True,
                        )
                    if item["tag"] == "correlate":
                        result["correlation"] = correlate_series(
                            result.get("series") or [],
                            result["signals"].get("client_p95_ms"),
                        )
                    output["scenarios"][name] = result
                    print(
                        f"  pub={pub_mode} client_p95="
                        f"{result['signals'].get('client_p95_ms')} "
                        f"server_p95={result['signals'].get('server_wall_p95_ms')} "
                        f"pool_p95={result['signals'].get('pool_wait_p95_ms')} "
                        f"writes={(result.get('publisher') or {}).get('publish_writes')} "
                        f"missed={result.get('missed_intervention_pattern')} "
                        f"sat={result.get('saturated')}",
                        flush=True,
                    )
            finally:
                stop_process(api_proc)
                port = choose_port()
                base_url = f"http://{host}:{port}"
                experiment = ResourceExperiment(database_url, base_url)
    finally:
        output["metadata"]["completed_at"] = utc_iso()
        await experiment.close()
        _ = embedded

    output["verdict_inputs"] = derive_hotpath_verdict(output["scenarios"])
    # Decision gate (evidence-based, conservative).
    # KEEP ASYNC, CHANGE SIGNAL requires a *better* candidate signal, not merely
    # that server wall failed. Without that evidence → EXPERIMENTAL ONLY.
    v = output["verdict_inputs"]
    hot = v.get("hotpath") or {}
    corr = v.get("correlation_summaries") or []
    better_candidate = any(
        c.get("missed_by_server_wall") and not c.get("pool_wait_also_below_warn")
        for c in corr
        if isinstance(c, dict)
    )
    if (
        hot.get("measurable_hotpath_gain")
        and better_candidate
        and not v.get("missed_intervention_reproduced")
    ):
        decision = "KEEP ASYNC, CHANGE SIGNAL"
        rationale = (
            "Async reduced hot-path cost and a non-wall candidate tracked client "
            "degradation better than server wall."
        )
    elif hot.get("measurable_hotpath_gain") and not v.get(
        "missed_intervention_reproduced"
    ):
        decision = "KEEP CURRENT EXPERIMENTAL SIGNAL"
        rationale = (
            "Async removes hot-path publication and server wall remained useful "
            "enough on this run."
        )
    elif better_candidate:
        decision = "KEEP ASYNC, CHANGE SIGNAL"
        rationale = (
            "Async is the publish default; promote a non-wall candidate only after "
            "a dedicated controller comparison."
        )
    else:
        decision = "EXPERIMENTAL ONLY"
        rationale = (
            "Async publish is the architectural default (request never awaits "
            "Postgres hint write). No cheap server-side signal reliably tracked "
            "client p95 under this topology (wall and pool-wait both stayed "
            "below warn while client saturated). Feedback thresholds stay off."
        )

    output["decision"] = {
        "gate": decision,
        "rationale": rationale,
        "product_defaults": {
            "resource_interactive_p95_warn_ms": 0.0,
            "resource_interactive_p95_critical_ms": 0.0,
            "resource_interactive_publish_mode": "async",
        },
    }

    path = ROOT / args.results_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(f"decision={decision}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("smoke", "hotpath", "missed", "correlate", "control", "full"),
        default="full",
    )
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--clients", type=int, default=0)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--publish-interval-ms", type=float, default=250.0)
    parser.add_argument(
        "--results-path", default="docs/interactive-hotpath-results.json"
    )
    args = parser.parse_args()
    mp.freeze_support()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

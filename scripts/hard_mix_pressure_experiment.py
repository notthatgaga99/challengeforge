#!/usr/bin/env python
"""Hard-mix resource pressure experiment: HTTP + workers + v3 quality.

Isolated processes:
  - API child OS process
  - evaluation worker child OS process
  - load generator = parent

Compares legacy / always_expensive / fixed_progressive / resource_aware_adaptive
under deterministic WorkloadKind mixes while interactive traffic runs.

Does not add Redis/Kafka/K8s. Does not manufacture capacity numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import random
import sys
import time
from collections import Counter, defaultdict
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
from challengeforge.evaluation.plan import ALWAYS_EXPENSIVE_COST_UNITS, EvaluationMode
from challengeforge.evaluation.workload_v3 import (
    Decision,
    WorkloadKind,
    decision_from_score,
    ground_truth_for,
    parse_workload_kind,
)
from challengeforge.main import create_app
from challengeforge.runtime.coalescing import InProcessCoalescer
from challengeforge.worker import EvaluationWorker
from concurrency_experiment import asyncpg_url, choose_port, percentile, utc_iso, wait_for_server
from interactive_isolated_profile import (
    clients_for_rate,
    saturation_reason,
    start_api_process,
    start_worker_process,
    stop_process,
)
from interactive_profile import DEFAULT_ENDPOINTS, drive, seed_world
from resource_capacity_experiment import ResourceExperiment, env_snapshot

# --- Safety caps (laptop) ---
MAX_EVALS_PER_SCENARIO = 24
MAX_INTERACTIVE_DURATION = 12.0
MAX_DRAIN_SECONDS = 45.0
MAX_CLIENTS = 100
DEFAULT_RATES = "10,20,40"

# Mix definitions: (kind, weight). Weights are relative; seed expands to counts.
MIX_WEIGHTS: dict[str, list[tuple[str, int]]] = {
    "mostly_easy": [
        ("easy_pass", 4),
        ("easy_fail", 4),
        ("ambiguous_pass", 1),
        ("ambiguous_fail", 1),
    ],
    "balanced": [
        ("easy_pass", 2),
        ("easy_fail", 2),
        ("ambiguous_pass", 2),
        ("ambiguous_fail", 1),
        ("hard_pass", 2),
        ("hard_fail", 1),
    ],
    "difficult": [
        ("easy_pass", 1),
        ("easy_fail", 1),
        ("ambiguous_pass", 2),
        ("ambiguous_fail", 2),
        ("hard_pass", 2),
        ("hard_fail", 2),
    ],
    "adversarial": [
        ("adversarial_pass", 3),
        ("adversarial_fail", 3),
        ("hard_pass", 2),
        ("hard_fail", 2),
    ],
    "heavy_dominated": [
        ("easy_pass", 1),
        ("easy_fail", 0),
        ("ambiguous_pass", 1),
        ("ambiguous_fail", 1),
        ("hard_pass", 4),
        ("hard_fail", 3),
    ],
    "hard_70": [
        ("easy_pass", 1),
        ("easy_fail", 1),
        ("ambiguous_pass", 1),
        ("hard_pass", 2),
        ("hard_fail", 2),
        ("adversarial_pass", 2),
        ("adversarial_fail", 1),
    ],
    "hard_90": [
        ("easy_pass", 1),
        ("hard_pass", 3),
        ("hard_fail", 2),
        ("adversarial_pass", 2),
        ("adversarial_fail", 2),
    ],
}

POLICIES = (
    EvaluationMode.LEGACY.value,
    EvaluationMode.ALWAYS_EXPENSIVE.value,
    EvaluationMode.FIXED_PROGRESSIVE.value,
    EvaluationMode.RESOURCE_AWARE_ADAPTIVE.value,
)

PRESSURE_LEVELS = ("low", "medium", "high", "burst")


def latency(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "mean_ms": round(sum(values) / len(values), 2) if values else 0.0,
        "max_ms": round(max(values), 2) if values else 0.0,
    }


def kind_to_workload_class(kind: str) -> WorkloadClass:
    key = kind.lower()
    if key.startswith("easy"):
        return WorkloadClass.LIGHT
    if key.startswith("ambiguous"):
        return WorkloadClass.MEDIUM
    return WorkloadClass.HEAVY


def expand_mix(mix_name: str, *, total: int, seed: int) -> list[str]:
    """Deterministic expansion of mix weights to exactly `total` kinds."""
    if mix_name not in MIX_WEIGHTS:
        raise ValueError(f"unknown mix: {mix_name}")
    total = max(1, min(total, MAX_EVALS_PER_SCENARIO))
    weights = MIX_WEIGHTS[mix_name]
    weight_sum = sum(w for _, w in weights) or 1
    counts: list[tuple[str, int]] = []
    assigned = 0
    for i, (kind, w) in enumerate(weights):
        if i == len(weights) - 1:
            n = max(0, total - assigned)
        else:
            n = max(0, round(total * w / weight_sum))
            assigned += n
        counts.append((kind, n))
    # Fix rounding drift.
    drift = total - sum(n for _, n in counts)
    if drift != 0 and counts:
        kind, n = counts[-1]
        counts[-1] = (kind, max(0, n + drift))
    kinds: list[str] = []
    for kind, n in counts:
        kinds.extend([kind] * n)
    rng = random.Random(seed)
    rng.shuffle(kinds)
    return kinds[:total]


def mix_summary(kinds: list[str]) -> dict[str, Any]:
    counts = Counter(kinds)
    n = len(kinds) or 1

    def share(*prefixes: str) -> float:
        return round(sum(counts[k] for k in counts if any(k.startswith(p) for p in prefixes)) / n, 4)

    return {
        "counts": dict(counts),
        "n": len(kinds),
        "easy_share": share("easy"),
        "ambiguous_share": share("ambiguous"),
        "hard_share": share("hard"),
        "adversarial_share": share("adversarial"),
        "hard_or_adversarial_share": share("hard", "adversarial"),
    }


async def create_submitted_kind(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    challenge_id: str,
    *,
    index: int,
    kind: str,
    evaluation_mode: str,
) -> dict[str, Any]:
    workload = kind_to_workload_class(kind)
    hdrs = dict(experiment.participant_headers[index % len(experiment.participant_headers)])
    hdrs["Idempotency-Key"] = f"hm-{evaluation_mode}-{kind}-{index}-{uuid4().hex[:8]}"
    created = await experiment.request(
        client,
        "POST",
        f"/api/v1/challenges/{challenge_id}/submissions",
        headers=hdrs,
        json={
            "metadata": {
                "workload_class": workload.value,
                "workload_kind": kind,
                "evaluation_mode": evaluation_mode,
                "i": index,
            }
        },
    )
    assert created.status in (200, 201), created
    t0 = time.perf_counter()
    submitted = await experiment.request(
        client,
        "POST",
        f"/api/v1/submissions/{created.body['id']}/submit",
        headers={"X-User-Id": hdrs["X-User-Id"]},
    )
    accept_ms = (time.perf_counter() - t0) * 1000.0
    assert submitted.status == 200, submitted
    body = dict(submitted.body)
    body["_accept_latency_ms"] = accept_ms
    body["_workload_kind"] = kind
    body["_evaluation_mode"] = evaluation_mode
    return body


async def enqueue_batch(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    challenge_id: str,
    kinds: list[str],
    *,
    evaluation_mode: str,
    index_base: int,
) -> tuple[list[str], list[float]]:
    ids: list[str] = []
    accepts: list[float] = []
    for i, kind in enumerate(kinds):
        body = await create_submitted_kind(
            experiment,
            client,
            challenge_id,
            index=index_base + i,
            kind=kind,
            evaluation_mode=evaluation_mode,
        )
        ids.append(body["id"])
        accepts.append(float(body["_accept_latency_ms"]))
    return ids, accepts


async def enqueue_sustained(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    challenge_id: str,
    kinds: list[str],
    *,
    evaluation_mode: str,
    index_base: int,
    rate: float,
    duration: float,
) -> tuple[list[str], list[float]]:
    """Enqueue at roughly `rate` submissions/sec until kinds exhausted or duration."""
    if rate <= 0 or not kinds:
        return [], []
    ids: list[str] = []
    accepts: list[float] = []
    interval = 1.0 / rate
    deadline = time.perf_counter() + duration
    for i, kind in enumerate(kinds):
        if time.perf_counter() >= deadline:
            break
        started = time.perf_counter()
        body = await create_submitted_kind(
            experiment,
            client,
            challenge_id,
            index=index_base + i,
            kind=kind,
            evaluation_mode=evaluation_mode,
        )
        ids.append(body["id"])
        accepts.append(float(body["_accept_latency_ms"]))
        await asyncio.sleep(max(0.0, interval - (time.perf_counter() - started)))
    return ids, accepts


async def wait_drain(
    experiment: ResourceExperiment,
    *,
    challenge_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.perf_counter() + timeout_seconds
    samples: list[dict[str, Any]] = []
    while time.perf_counter() < deadline:
        async with experiment.worker_sessions() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT
                          count(*) FILTER (WHERE e.status = 'queued') AS queued,
                          count(*) FILTER (WHERE e.status = 'running') AS running,
                          count(*) FILTER (WHERE e.status = 'succeeded') AS succeeded,
                          count(*) FILTER (WHERE e.status = 'failed') AS failed
                        FROM evaluations e
                        JOIN submissions s ON s.id = e.submission_id
                        WHERE s.challenge_id = :cid
                        """
                    ),
                    {"cid": challenge_id},
                )
            ).mappings().one()
            runtime = (
                await session.execute(
                    text(
                        """
                        SELECT pressure_state, adaptive_max_workers
                        FROM evaluation_scheduler_state
                        WHERE id = 1
                        """
                    )
                )
            ).mappings().first()
            await session.rollback()
        sample = {
            "t": round(time.perf_counter(), 3),
            "queued": int(row["queued"]),
            "running": int(row["running"]),
            "succeeded": int(row["succeeded"]),
            "failed": int(row["failed"]),
            "runtime": (
                {
                    "pressure_state": runtime["pressure_state"],
                    "adaptive_max_workers": runtime["adaptive_max_workers"],
                }
                if runtime
                else None
            ),
        }
        samples.append(sample)
        if int(row["queued"]) == 0 and int(row["running"]) == 0:
            break
        await asyncio.sleep(0.25)
    return {
        "samples": samples,
        "max_queue_depth": max((s["queued"] for s in samples), default=0),
        "ending_queued": samples[-1]["queued"] if samples else 0,
        "ending_running": samples[-1]["running"] if samples else 0,
        "pressure_states_seen": sorted(
            {
                (s["runtime"] or {}).get("pressure_state")
                for s in samples
                if s.get("runtime")
            }
            - {None}
        ),
        "adaptive_max_seen": [
            (s["runtime"] or {}).get("adaptive_max_workers")
            for s in samples
            if s.get("runtime")
        ],
    }


async def collect_evaluation_quality(
    experiment: ResourceExperiment,
    challenge_id: str,
) -> dict[str, Any]:
    async with experiment.worker_sessions() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT s.id AS submission_id,
                           s.metadata AS submission_metadata,
                           e.status,
                           e.score,
                           e.workload_class,
                           e.evaluation_mode,
                           e.current_stage,
                           e.result_metadata,
                           e.created_at,
                           e.started_at,
                           e.completed_at,
                           e.attempt_count
                    FROM evaluations e
                    JOIN submissions s ON s.id = e.submission_id
                    WHERE s.challenge_id = :cid
                    ORDER BY e.created_at, e.id
                    """
                ),
                {"cid": challenge_id},
            )
        ).mappings().all()
        await session.rollback()

    waits: list[float] = []
    execs: list[float] = []
    agreements = 0
    compared = 0
    fep = 0
    fef = 0
    early = 0
    escalated = 0
    heavy = 0
    cost_units = 0
    mode_na_legacy = 0
    stranded = 0
    by_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "agree": 0})

    for r in rows:
        if r["started_at"] and r["created_at"]:
            waits.append((r["started_at"] - r["created_at"]).total_seconds() * 1000)
        if r["completed_at"] and r["started_at"]:
            execs.append((r["completed_at"] - r["started_at"]).total_seconds() * 1000)
        if r["status"] in ("queued", "running"):
            stranded += 1

        meta = r["submission_metadata"] or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        kind = parse_workload_kind(meta if isinstance(meta, dict) else {})
        mode = str(r["evaluation_mode"] or "legacy")
        result_meta = r["result_metadata"] or {}
        if isinstance(result_meta, str):
            result_meta = json.loads(result_meta)
        progressive = (
            result_meta.get("progressive")
            if isinstance(result_meta, dict)
            else None
        )
        stages_completed: list[Any] = []
        actual_cost = 0
        early_reason = None
        if isinstance(progressive, dict):
            stages_completed = list(progressive.get("stages_completed") or [])
            actual_cost = int(progressive.get("actual_cost_units") or 0)
            early_reason = progressive.get("early_exit_reason")

        if mode == EvaluationMode.LEGACY.value:
            mode_na_legacy += 1
            continue
        if kind is None or r["status"] != "succeeded" or r["score"] is None:
            continue

        compared += 1
        sid = r["submission_id"]
        if not isinstance(sid, UUID):
            sid = UUID(str(sid))
        truth = ground_truth_for(kind, sid)
        decision = decision_from_score(int(r["score"]))
        agree = decision == truth.decision
        if agree:
            agreements += 1
        by_kind[kind.value]["n"] += 1
        if agree:
            by_kind[kind.value]["agree"] += 1

        stage_count = len(stages_completed) if stages_completed else int(r["current_stage"] or 0)
        is_early = bool(
            early_reason
            and str(early_reason).startswith("safe_early_exit")
            and stage_count < 3
        )
        if is_early:
            early += 1
        if stage_count > 1:
            escalated += 1
        stage_names = []
        for s in stages_completed:
            if isinstance(s, dict):
                stage_names.append(str(s.get("stage") or ""))
            else:
                stage_names.append(str(s))
        if stage_count >= 3 or "heavy" in stage_names:
            heavy += 1
        if is_early and decision == Decision.PASS and truth.decision == Decision.FAIL:
            fep += 1
        if is_early and decision == Decision.FAIL and truth.decision == Decision.PASS:
            fef += 1
        if actual_cost:
            cost_units += actual_cost
        elif mode == EvaluationMode.ALWAYS_EXPENSIVE.value:
            cost_units += ALWAYS_EXPENSIVE_COST_UNITS
        else:
            # Infer from stage count if metadata missing.
            inferred = {1: 1, 2: 4, 3: ALWAYS_EXPENSIVE_COST_UNITS}.get(
                stage_count, ALWAYS_EXPENSIVE_COST_UNITS
            )
            cost_units += inferred

    completed = sum(1 for r in rows if r["status"] in ("succeeded", "failed"))
    always_cost = ALWAYS_EXPENSIVE_COST_UNITS * compared if compared else 0
    return {
        "enqueued": len(rows),
        "completed": completed,
        "succeeded": sum(1 for r in rows if r["status"] == "succeeded"),
        "failed": sum(1 for r in rows if r["status"] == "failed"),
        "stranded": stranded,
        "queue_wait_ms": latency(waits),
        "execution_ms": latency(execs),
        "quality": {
            "compared": compared,
            "legacy_skipped": mode_na_legacy,
            "decision_agreement": round(agreements / compared, 4) if compared else None,
            "false_early_pass": fep,
            "false_early_fail": fef,
            "early_exit_rate": round(early / compared, 4) if compared else None,
            "escalation_rate": round(escalated / compared, 4) if compared else None,
            "heavy_stage_invocation_rate": round(heavy / compared, 4) if compared else None,
            "actual_cost_units": cost_units,
            "always_expensive_cost_units": always_cost,
            "compute_savings": (
                round(1.0 - cost_units / always_cost, 4) if always_cost else None
            ),
            "by_kind": dict(by_kind),
            "note": (
                "legacy mode is not bound to v3 ground truth; agreement is null for A"
            ),
        },
    }


def _worker_process_main_configured(
    database_url: str,
    artifact_root: str,
    worker_count: int,
    stop_event: Any,
    settings_overrides: dict[str, Any],
) -> None:
    import asyncio

    from challengeforge.persistence.session import create_engine as create_cf_engine
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
                settings,
                worker_id=f"hm-w{i}",
                session_factory=sessions,
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


def start_configured_worker(
    database_url: str,
    artifact_root: Path,
    worker_count: int,
    settings_overrides: dict[str, Any] | None = None,
) -> tuple[mp.Process, Any]:
    ctx = mp.get_context("spawn")
    stop = ctx.Event()
    process = ctx.Process(
        target=_worker_process_main_configured,
        args=(
            database_url,
            str(artifact_root),
            worker_count,
            stop,
            settings_overrides or {},
        ),
        daemon=True,
        name="challengeforge-hard-mix-workers",
    )
    process.start()
    return process, stop


async def run_scenario(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    *,
    name: str,
    mix_name: str,
    evaluation_mode: str,
    interactive_rps: float,
    interactive_duration: float,
    pressure_level: str,
    eval_count: int,
    eval_arrival_rps: float,
    clients: int,
    workers: int,
    server_pid: int,
    database_url: str,
    artifact_root: Path,
    seed: int,
    http_timeout: float,
    drain_after: bool,
) -> dict[str, Any]:
    await experiment.prepare_database()
    world = await seed_world(experiment, client, dataset="small")
    browse_id = world["challenge_id"]
    _, pressure_id = await experiment.create_published_challenge(
        client, f"hm-{name[:40]}"
    )

    kinds = expand_mix(mix_name, total=eval_count, seed=seed)
    accept_latencies: list[float] = []
    submission_ids: list[str] = []

    # Pre-enqueue for low / high / burst; medium uses sustained during interactive.
    pre_kinds: list[str] = []
    sustained_kinds: list[str] = []
    if pressure_level == "low":
        pre_kinds = kinds[: max(1, len(kinds) // 4)]
    elif pressure_level == "medium":
        sustained_kinds = kinds
    elif pressure_level == "high":
        pre_kinds = kinds
    elif pressure_level == "burst":
        pre_kinds = kinds
    else:
        raise ValueError(f"unknown pressure_level: {pressure_level}")

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

    worker_proc, worker_stop = start_configured_worker(
        database_url,
        artifact_root,
        workers,
        {
            "evaluation_max_workers": max(1, workers),
            "evaluation_min_workers": 1,
            "evaluation_max_concurrent_heavy": 1,
            "resource_aware_runtime_enabled": True,
        },
    )
    await asyncio.sleep(0.15)

    api_proc = psutil.Process(server_pid)
    lg_proc = psutil.Process()
    api_proc.cpu_percent(None)
    lg_proc.cpu_percent(None)
    worker_ps = psutil.Process(worker_proc.pid) if worker_proc.is_alive() else None
    if worker_ps:
        worker_ps.cpu_percent(None)

    t0 = time.perf_counter()
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
                duration=interactive_duration,
            )
        )

    interactive = await drive(
        client,
        experiment,
        world,
        endpoints=[e.strip() for e in DEFAULT_ENDPOINTS.split(",")],
        rate=interactive_rps,
        duration=interactive_duration,
        clients=clients,
        server_pid=server_pid,
        http_timeout_seconds=http_timeout,
        load_generator_note=(
            "hard-mix: API / workers / load-gen are separate OS processes; "
            "shared laptop host"
        ),
    )

    if sustained_task is not None:
        ids, accepts = await sustained_task
        submission_ids.extend(ids)
        accept_latencies.extend(accepts)

    arrival_elapsed = max(0.001, time.perf_counter() - t0)
    arrival_rate = len(submission_ids) / arrival_elapsed

    drain: dict[str, Any] | None = None
    if drain_after:
        drain = await wait_drain(
            experiment, challenge_id=pressure_id, timeout_seconds=MAX_DRAIN_SECONDS
        )

    quality_block = await collect_evaluation_quality(experiment, pressure_id)

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

    # Sample resources before tearing down the worker process.
    api_cpu = _safe_cpu(api_proc)
    lg_cpu = _safe_cpu(lg_proc)
    worker_cpu = _safe_cpu(worker_ps)
    api_rss = _safe_rss(api_proc)
    lg_rss = _safe_rss(lg_proc)

    stop_process(worker_proc, worker_stop)

    # If we did not drain with workers alive, brief grace then snapshot.
    if not drain_after:
        await asyncio.sleep(0.3)

    async with experiment.db.connect() as conn:
        db_conn = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                )
            )
        ).scalar_one()

    sat = saturation_reason(interactive, interactive_rps)
    completed = int(quality_block["completed"])
    wall = max(0.001, time.perf_counter() - t0)

    return {
        "name": name,
        "mix": mix_name,
        "mix_summary": mix_summary(kinds),
        "evaluation_mode": evaluation_mode,
        "pressure_level": pressure_level,
        "interactive": {
            "offered_rps": interactive_rps,
            "duration_seconds": interactive_duration,
            "clients": clients,
            "browse_challenge_id": browse_id,
            **interactive,
        },
        "saturation_reason": sat,
        "saturated": sat is not None,
        "evaluation": {
            "pressure_challenge_id": pressure_id,
            "planned_kinds": len(kinds),
            "arrived": len(submission_ids),
            "arrival_rate_per_second": round(arrival_rate, 3),
            "completion_rate_per_second": round(completed / wall, 3),
            "accept_latency_ms": latency(accept_latencies),
            **quality_block,
        },
        "drain": drain,
        "resources": {
            "api_pid": server_pid,
            "worker_pid": worker_proc.pid,
            "load_generator_pid": lg_proc.pid,
            "api_cpu_percent": api_cpu,
            "worker_cpu_percent": worker_cpu,
            "load_generator_cpu_percent": lg_cpu,
            "api_rss_mb": round(api_rss, 2) if api_rss is not None else None,
            "load_generator_rss_mb": round(lg_rss, 2) if lg_rss is not None else None,
            "db_connections": int(db_conn),
            "workers_configured": workers,
        },
        "seed": seed,
    }


async def coalescing_bench() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for n in (10, 50, 100):
        coal = InProcessCoalescer()
        calls = {"n": 0}

        async def compute():
            calls["n"] += 1
            await asyncio.sleep(0.02)
            return "ok"

        t0 = time.perf_counter()
        await asyncio.gather(*[coal.do("k", compute) for _ in range(n)])
        out[str(n)] = {
            "waiters": n,
            "computations": calls["n"],
            "joins": coal.stats.joins,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "limitation": (
                "in-process only; not coordinated across multiple API processes; "
                "not on the default HTTP evaluation path"
            ),
        }
    return out


def portfolio_plan() -> list[dict[str, Any]]:
    """Bounded matrix for a laptop commit run."""
    scenarios: list[dict[str, Any]] = []
    # 1) Full policy × representative mixes at 20 RPS, medium pressure
    for mix in (
        "mostly_easy",
        "balanced",
        "difficult",
        "adversarial",
        "heavy_dominated",
        "hard_70",
        "hard_90",
    ):
        for mode in POLICIES:
            scenarios.append(
                {
                    "mix": mix,
                    "mode": mode,
                    "rps": 20.0,
                    "pressure": "medium",
                    "eval_count": 12 if mix.startswith("hard") else 10,
                    "eval_arrival_rps": 2.0,
                    "tag": "policy_mix",
                }
            )
    # 2) Rate sweep on difficult + fixed_progressive (stop early on saturation)
    for rps in (10.0, 20.0, 40.0, 60.0):
        scenarios.append(
            {
                "mix": "difficult",
                "mode": EvaluationMode.FIXED_PROGRESSIVE.value,
                "rps": rps,
                "pressure": "medium",
                "eval_count": 10,
                "eval_arrival_rps": 2.0,
                "tag": "rate_sweep",
            }
        )
    # 3) Pressure levels on hard_70 for fixed + adaptive
    for pressure in ("low", "high", "burst"):
        for mode in (
            EvaluationMode.FIXED_PROGRESSIVE.value,
            EvaluationMode.RESOURCE_AWARE_ADAPTIVE.value,
        ):
            scenarios.append(
                {
                    "mix": "hard_70",
                    "mode": mode,
                    "rps": 20.0,
                    "pressure": pressure,
                    "eval_count": 16 if pressure in ("high", "burst") else 12,
                    "eval_arrival_rps": 4.0 if pressure == "high" else 2.0,
                    "tag": "pressure_sweep",
                }
            )
    # 4) Abusive invariant: adversarial under interactive + high eval pressure
    scenarios.append(
        {
            "mix": "adversarial",
            "mode": EvaluationMode.FIXED_PROGRESSIVE.value,
            "rps": 20.0,
            "pressure": "high",
            "eval_count": 20,
            "eval_arrival_rps": 3.0,
            "tag": "abusive_invariant",
        }
    )
    # 5) Recovery: burst then drain under adaptive
    scenarios.append(
        {
            "mix": "difficult",
            "mode": EvaluationMode.RESOURCE_AWARE_ADAPTIVE.value,
            "rps": 20.0,
            "pressure": "burst",
            "eval_count": 18,
            "eval_arrival_rps": 1.0,
            "tag": "recovery",
        }
    )
    return scenarios


def smoke_plan() -> list[dict[str, Any]]:
    return [
        {
            "mix": "difficult",
            "mode": EvaluationMode.FIXED_PROGRESSIVE.value,
            "rps": 10.0,
            "pressure": "low",
            "eval_count": 6,
            "eval_arrival_rps": 1.0,
            "tag": "smoke",
        }
    ]


def summarize_scorecard(scenarios: dict[str, Any]) -> dict[str, Any]:
    card: dict[str, Any] = {}
    for name, sc in scenarios.items():
        q = (sc.get("evaluation") or {}).get("quality") or {}
        inter = sc.get("interactive") or {}
        card[name] = {
            "tag": sc.get("tag"),
            "mix": sc.get("mix"),
            "mode": sc.get("evaluation_mode"),
            "pressure": sc.get("pressure_level"),
            "offered_rps": inter.get("offered_rps") or inter.get("offered_requests_per_second"),
            "achieved_rps": inter.get("achieved_requests_per_second"),
            "interactive_p95_ms": (inter.get("client_latency_ms") or {}).get("p95_ms"),
            "interactive_errors": inter.get("error_count"),
            "saturated": sc.get("saturated"),
            "decision_agreement": q.get("decision_agreement"),
            "false_early_pass": q.get("false_early_pass"),
            "false_early_fail": q.get("false_early_fail"),
            "compute_savings": q.get("compute_savings"),
            "early_exit_rate": q.get("early_exit_rate"),
            "escalation_rate": q.get("escalation_rate"),
            "heavy_rate": q.get("heavy_stage_invocation_rate"),
            "eval_completed": (sc.get("evaluation") or {}).get("completed"),
            "stranded": (sc.get("evaluation") or {}).get("stranded"),
            "accept_p95_ms": ((sc.get("evaluation") or {}).get("accept_latency_ms") or {}).get(
                "p95_ms"
            ),
        }
    return card


def derive_verdict(scorecard: dict[str, Any]) -> dict[str, Any]:
    prog = [
        v
        for v in scorecard.values()
        if v.get("mode") == EvaluationMode.FIXED_PROGRESSIVE.value
        and v.get("tag") == "policy_mix"
    ]
    adaptive = [
        v
        for v in scorecard.values()
        if v.get("mode") == EvaluationMode.RESOURCE_AWARE_ADAPTIVE.value
        and v.get("tag") == "policy_mix"
    ]
    agreements = [v["decision_agreement"] for v in prog if v.get("decision_agreement") is not None]
    savings = [v["compute_savings"] for v in prog if v.get("compute_savings") is not None]
    hard = [
        v
        for v in prog
        if v.get("mix") in ("adversarial", "hard_70", "hard_90", "heavy_dominated")
    ]
    hard_savings = [v["compute_savings"] for v in hard if v.get("compute_savings") is not None]
    interactive_ok = all(
        (v.get("interactive_errors") or 0) == 0 and not v.get("saturated")
        for v in prog
        if v.get("offered_rps") == 20.0
    )
    fep = sum(int(v.get("false_early_pass") or 0) for v in prog)
    adaptive_savings = [
        v["compute_savings"] for v in adaptive if v.get("compute_savings") is not None
    ]
    mean = (lambda xs: round(sum(xs) / len(xs), 4) if xs else None)
    return {
        "fixed_progressive_mean_agreement": mean(agreements),
        "fixed_progressive_mean_savings": mean(savings),
        "hard_mix_mean_savings": mean(hard_savings),
        "false_early_pass_total": fep,
        "interactive_healthy_at_20rps": interactive_ok,
        "adaptive_mean_savings": mean(adaptive_savings),
        "adaptive_vs_fixed_savings_delta": (
            round(mean(adaptive_savings) - mean(savings), 4)
            if adaptive_savings and savings
            else None
        ),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from pgserver import get_server

    if args.profile == "smoke":
        plan = smoke_plan()
    elif args.profile == "portfolio":
        plan = portfolio_plan()
    else:
        plan = portfolio_plan()

    rates_override = None
    if args.rates:
        rates_override = [float(x) for x in args.rates.split(",") if x.strip()]

    data_dir = ROOT / ".pgserver-hard-mix-pressure"
    data_dir.mkdir(exist_ok=True)
    embedded = get_server(str(data_dir))
    database_url = asyncpg_url(embedded.get_uri())
    port = choose_port()
    host = "127.0.0.1"
    base_url = f"http://{host}:{port}"
    artifact_root = ROOT / "data" / "hard-mix-artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)

    experiment = ResourceExperiment(database_url, base_url)
    await experiment.prepare_database()

    api_proc, ready = start_api_process(database_url, host, port, artifact_root)
    ready.wait(timeout=30)
    await wait_for_server(base_url)
    if not api_proc.is_alive():
        raise RuntimeError("API child process exited before load generation")

    output: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "python": sys.version.split()[0],
            "isolation": (
                "api_separate_os_process; workers_third_process; "
                "load_generator_parent"
            ),
            "profile": args.profile,
            "api_pid": api_proc.pid,
            "load_generator_pid": psutil.Process().pid,
            "safety": {
                "max_evals_per_scenario": MAX_EVALS_PER_SCENARIO,
                "max_interactive_duration": MAX_INTERACTIVE_DURATION,
                "max_drain_seconds": MAX_DRAIN_SECONDS,
            },
            "ground_truth": "WorkloadKind suffix; decision = score >= 60",
            "safe_early_exit": "confidence AND safe_to_terminate",
            "claim": (
                "Hard-mix HTTP+worker pressure validation — not AI quality"
            ),
        },
        "scenarios": {},
        "scorecard": {},
        "coalescing": {},
        "stopped_early": None,
        "verdict_inputs": {},
    }

    duration = min(max(args.duration, 2.0), MAX_INTERACTIVE_DURATION)
    workers = max(1, args.workers)
    http_timeout = float(args.http_timeout)
    stop_rate_sweep = False
    seed = 42

    try:
        async with experiment.db.connect() as connection:
            output["metadata"]["postgresql_version"] = (
                await connection.execute(text("SHOW server_version"))
            ).scalar_one()

        for i, item in enumerate(plan):
            if item.get("tag") == "rate_sweep" and stop_rate_sweep:
                continue
            if rates_override and item.get("tag") == "rate_sweep":
                if item["rps"] not in rates_override:
                    continue
            rps = float(item["rps"])
            clients = (
                min(MAX_CLIENTS, max(1, args.clients))
                if args.clients > 0
                else clients_for_rate(rps)
            )
            name = (
                f"{item['tag']}__{item['mode']}__{item['mix']}__"
                f"p{item['pressure']}__rps{int(rps)}"
            )
            print(f"scenario {name}", flush=True)
            limits = httpx.Limits(
                max_connections=max(clients, 16),
                max_keepalive_connections=min(max(clients, 16), 40),
            )
            timeout = httpx.Timeout(http_timeout)
            async with httpx.AsyncClient(
                base_url=base_url, limits=limits, timeout=timeout
            ) as client:
                result = await run_scenario(
                    experiment,
                    client,
                    name=name,
                    mix_name=item["mix"],
                    evaluation_mode=item["mode"],
                    interactive_rps=rps,
                    interactive_duration=duration,
                    pressure_level=item["pressure"],
                    eval_count=min(item["eval_count"], MAX_EVALS_PER_SCENARIO),
                    eval_arrival_rps=float(item["eval_arrival_rps"]),
                    clients=clients,
                    workers=workers,
                    server_pid=api_proc.pid,
                    database_url=database_url,
                    artifact_root=artifact_root,
                    seed=seed + i,
                    http_timeout=http_timeout,
                    drain_after=True,
                )
            result["tag"] = item["tag"]
            output["scenarios"][name] = result
            q = result["evaluation"]["quality"]
            print(
                f"  achieved={result['interactive'].get('achieved_requests_per_second')} "
                f"p95={result['interactive'].get('client_latency_ms', {}).get('p95_ms')} "
                f"agree={q.get('decision_agreement')} "
                f"savings={q.get('compute_savings')} "
                f"saturated={result['saturated']}",
                flush=True,
            )
            if (
                item.get("tag") == "rate_sweep"
                and result["saturated"]
                and not args.continue_on_saturation
            ):
                output["stopped_early"] = {
                    "at_rate": rps,
                    "scenario": name,
                    "reason": result["saturation_reason"],
                }
                stop_rate_sweep = True

        output["coalescing"] = await coalescing_bench()
        output["scorecard"] = summarize_scorecard(output["scenarios"])
        output["verdict_inputs"] = derive_verdict(output["scorecard"])
    finally:
        output["metadata"]["completed_at"] = utc_iso()
        stop_process(api_proc)
        await experiment.close()
        _ = embedded

    results_path = ROOT / args.results_path
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {results_path}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("smoke", "portfolio", "full"),
        default="portfolio",
    )
    parser.add_argument("--duration", type=float, default=6.0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--clients", type=int, default=0)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    parser.add_argument("--rates", default="", help="Optional rate-sweep filter, e.g. 10,20,40")
    parser.add_argument("--continue-on-saturation", action="store_true")
    parser.add_argument(
        "--results-path",
        default="docs/hard-mix-pressure-results.json",
    )
    args = parser.parse_args()
    mp.freeze_support()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

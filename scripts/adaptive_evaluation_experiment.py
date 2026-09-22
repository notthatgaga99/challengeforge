#!/usr/bin/env python
"""Adaptive Evaluation v2 experiment: A/B/C progressive modes + savings.

Measures compute_savings vs always-expensive baseline under synthetic scenarios.
Bounded for laptop safety.
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
from challengeforge.evaluation.plan import ALWAYS_EXPENSIVE_COST_UNITS, EvaluationMode
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

MAX_JOBS = 24


def latency(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
    }


SCENARIO_MIXES = {
    "all_confident_pass": [("pass_confident", 12)],
    "all_uncertain": [("uncertain", 12)],
    "fifty_fifty": [("pass_confident", 6), ("uncertain", 6)],
    "eighty_twenty": [("pass_confident", 10), ("requires_expensive", 2)],
}


async def run_mode(
    experiment: ResourceExperiment,
    client: httpx.AsyncClient,
    *,
    mode: EvaluationMode,
    mix_name: str,
    pressure_cpu_high: float,
) -> dict[str, Any]:
    await experiment.prepare_database()
    _, challenge_id = await experiment.create_published_challenge(
        client, f"ae2-{mode.value}-{mix_name}"
    )
    jobs: list[str] = []
    index = 0
    for scenario, count in SCENARIO_MIXES[mix_name]:
        for _ in range(min(count, MAX_JOBS - len(jobs))):
            headers = dict(
                experiment.participant_headers[index % len(experiment.participant_headers)]
            )
            headers["Idempotency-Key"] = f"ae2-{mode.value}-{index}-{uuid4().hex[:6]}"
            created = await experiment.request(
                client,
                "POST",
                f"/api/v1/challenges/{challenge_id}/submissions",
                headers=headers,
                json={
                    "metadata": {
                        "workload_class": WorkloadClass.LIGHT.value,
                        "adaptive_scenario": scenario,
                        "evaluation_mode": mode.value,
                    }
                },
            )
            assert created.status in (200, 201), created
            submitted = await experiment.request(
                client,
                "POST",
                f"/api/v1/submissions/{created.body['id']}/submit",
                headers=headers,
            )
            assert submitted.status in (200, 201), submitted
            jobs.append(created.body["id"])
            index += 1

    settings = experiment.worker_settings.model_copy(
        update={
            "evaluation_progressive_mode": mode.value,
            "evaluation_max_workers": 2,
            "evaluation_min_workers": 1,
            "resource_aware_runtime_enabled": True,
            "resource_cpu_high_percent": pressure_cpu_high,
            "evaluation_poll_interval_seconds": 0.05,
            "resource_adjust_cooldown_seconds": 0.5,
        }
    )
    worker = EvaluationWorker(
        settings,
        worker_id=f"ae2-{mode.value}",
        session_factory=experiment.worker_sessions,
    )
    # Simulate elevated pressure for adaptive deferral scenarios by setting hint
    # when high threshold is low.
    if pressure_cpu_high <= 5:
        worker.runtime.set_interactive_p95_ms(5000)

    task = asyncio.create_task(worker.run_forever())
    proc = psutil.Process()
    proc.cpu_percent(None)
    t0 = time.perf_counter()

    # Interactive burst
    inter_lat: list[float] = []
    inter_err = 0
    for i in range(20):
        headers = experiment.participant_headers[i % len(experiment.participant_headers)]
        started = time.perf_counter()
        try:
            response = await client.get(
                f"/api/v1/challenges/{challenge_id}", headers=headers
            )
            inter_lat.append((time.perf_counter() - started) * 1000.0)
            if response.status_code >= 400:
                inter_err += 1
        except Exception:
            inter_err += 1

    await worker.drain(max_idle_rounds=5, timeout_seconds=60)
    # Second drain pass for deferred escalations under adaptive mode.
    if mode == EvaluationMode.RESOURCE_AWARE_ADAPTIVE:
        worker.runtime.set_interactive_p95_ms(None)
        # Raise CPU high threshold so NORMAL allows deferred HEAVY.
        worker.runtime.budget = worker.runtime.budget  # noqa: keep
        from challengeforge.runtime.budgets import ResourceBudget

        worker.runtime.budget = ResourceBudget.from_settings(
            settings.model_copy(update={"resource_cpu_high_percent": 95.0,
                                         "resource_interactive_p95_critical_ms": 0.0})
        )
        worker.runtime.classifier  # touch
        await worker.drain(max_idle_rounds=5, timeout_seconds=60)

    worker.request_stop()
    await asyncio.gather(task, return_exceptions=True)
    elapsed = time.perf_counter() - t0
    cpu = proc.cpu_percent(None)
    rss = proc.memory_info().rss / (1024 * 1024)

    async with experiment.worker_sessions() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT status, result_metadata, current_stage
                    FROM evaluations
                    """
                )
            )
        ).all()
        await session.rollback()

    succeeded = [r for r in rows if r[0] == "succeeded"]
    queued = [r for r in rows if r[0] == "queued"]
    total_cost = 0
    early_exits = 0
    escalations = 0
    for status, meta, stage in succeeded:
        prog = (meta or {}).get("progressive") or {}
        total_cost += int(prog.get("actual_cost_units") or 0)
        if prog.get("early_exit_reason", "").startswith("early_exit_"):
            early_exits += 1
        if int(prog.get("stages_attempted") or 0) > 1:
            escalations += 1

    always_cost = ALWAYS_EXPENSIVE_COST_UNITS * len(jobs)
    return {
        "mode": mode.value,
        "mix": mix_name,
        "jobs": len(jobs),
        "succeeded": len(succeeded),
        "queued_remaining": len(queued),
        "elapsed_seconds": round(elapsed, 3),
        "evaluation_throughput_per_second": round(
            len(succeeded) / elapsed if elapsed else 0.0, 3
        ),
        "interactive": {
            "errors": inter_err,
            "latency_ms": latency(inter_lat),
        },
        "early_exit_rate": round(early_exits / max(1, len(succeeded)), 4),
        "escalation_rate": round(escalations / max(1, len(succeeded)), 4),
        "actual_cost_units": total_cost,
        "always_expensive_cost_units": always_cost,
        "compute_savings": round(1.0 - (total_cost / always_cost), 4)
        if always_cost
        else 0.0,
        "process": {"cpu_percent": cpu, "rss_mb": round(rss, 2)},
        "submissions_durable": len(jobs),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from pgserver import get_server

    data_dir = ROOT / ".pgserver-adaptive-eval"
    data_dir.mkdir(exist_ok=True)
    embedded = get_server(str(data_dir))
    database_url = asyncpg_url(embedded.get_uri())
    port = choose_port()
    base_url = f"http://127.0.0.1:{port}"
    experiment = ResourceExperiment(database_url, base_url)
    await experiment.prepare_database()
    api = Settings(
        database_url=database_url,
        artifact_root=ROOT / "data" / "adaptive-eval-artifacts",
        log_level="WARNING",
        evaluation_progressive_mode="legacy",
    )
    server = UvicornThread(create_app(api), "127.0.0.1", port)
    server.start()
    await wait_for_server(base_url)

    output: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "always_expensive_cost_units_per_job": ALWAYS_EXPENSIVE_COST_UNITS,
            "compute_savings_definition": (
                "1 - sum(actual_cost_units) / (13 * jobs)"
            ),
            "claim": (
                "Resource-aware progressive evaluation architecture with "
                "deterministic synthetic stages — not an intelligent evaluator."
            ),
        },
        "scenarios": {},
        "scorecard": {},
    }
    modes = [
        EvaluationMode.ALWAYS_EXPENSIVE,
        EvaluationMode.FIXED_PROGRESSIVE,
        EvaluationMode.RESOURCE_AWARE_ADAPTIVE,
    ]
    mixes = ["all_confident_pass", "fifty_fifty", "eighty_twenty"]
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
            for mix in mixes:
                for mode in modes:
                    # High pressure only for adaptive on requires_expensive-heavy mixes
                    pressure = 95.0
                    if mode == EvaluationMode.RESOURCE_AWARE_ADAPTIVE and mix == "eighty_twenty":
                        pressure = 1.0  # force degraded via interactive hint
                    key = f"{mode.value}__{mix}"
                    print(f"scenario {key}", flush=True)
                    output["scenarios"][key] = await run_mode(
                        experiment,
                        client,
                        mode=mode,
                        mix_name=mix,
                        pressure_cpu_high=pressure,
                    )
    finally:
        output["metadata"]["completed_at"] = utc_iso()
        server.stop()
        await experiment.close()
        _ = embedded

    for key, sc in output["scenarios"].items():
        output["scorecard"][key] = {
            "compute_savings": sc["compute_savings"],
            "early_exit_rate": sc["early_exit_rate"],
            "escalation_rate": sc["escalation_rate"],
            "interactive_p95_ms": sc["interactive"]["latency_ms"]["p95_ms"],
            "eval_rps": sc["evaluation_throughput_per_second"],
            "succeeded": sc["succeeded"],
            "queued_remaining": sc["queued_remaining"],
        }

    path = ROOT / args.results_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-path", default="docs/adaptive-evaluation-results.json"
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

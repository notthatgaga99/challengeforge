#!/usr/bin/env python
"""Adversarial Adaptive Evaluation v3 — quality × cost matrix.

Compares always_expensive / fixed_progressive / resource_aware_adaptive under
harder deterministic workload kinds. Measures decision agreement vs ground truth.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from cf_experiment_paths import ensure_experiment_paths

ensure_experiment_paths()

from challengeforge.evaluation.plan import ALWAYS_EXPENSIVE_COST_UNITS, EvaluationMode
from challengeforge.evaluation.quality import compare_to_ground_truth
from challengeforge.evaluation.workload_v3 import WorkloadKind
from challengeforge.runtime.coalescing import InProcessCoalescer
from challengeforge.runtime.pressure import PressureState
from concurrency_experiment import percentile, utc_iso
from resource_capacity_experiment import env_snapshot

MIXES: dict[str, list[tuple[str, int]]] = {
    "easy": [("easy_pass", 8), ("easy_fail", 8), ("ambiguous_pass", 2), ("ambiguous_fail", 2)],
    "balanced": [
        ("easy_pass", 4),
        ("easy_fail", 4),
        ("ambiguous_pass", 3),
        ("ambiguous_fail", 3),
        ("hard_pass", 3),
        ("hard_fail", 3),
    ],
    "difficult": [
        ("easy_pass", 2),
        ("easy_fail", 2),
        ("ambiguous_pass", 4),
        ("ambiguous_fail", 4),
        ("hard_pass", 4),
        ("hard_fail", 4),
    ],
    "adversarial": [
        ("adversarial_pass", 6),
        ("adversarial_fail", 6),
        ("hard_pass", 4),
        ("hard_fail", 4),
    ],
    "heavy_dominated": [
        ("easy_pass", 1),
        ("easy_fail", 1),
        ("ambiguous_pass", 2),
        ("ambiguous_fail", 2),
        ("hard_pass", 7),
        ("hard_fail", 7),
    ],
}


def run_mix(
    mix_name: str,
    mode: EvaluationMode,
    pressure: PressureState,
) -> dict[str, Any]:
    jobs: list[tuple[str, Any]] = []
    for kind, count in MIXES[mix_name]:
        for _ in range(count):
            jobs.append((kind, uuid4()))

    t0 = time.perf_counter()
    comparisons = []
    for kind, sid in jobs:
        comparisons.append(
            compare_to_ground_truth(
                submission_id=sid,
                metadata={"workload_kind": kind},
                mode=mode,
                pressure=pressure,
            )
        )
    elapsed = time.perf_counter() - t0

    n = len(comparisons)
    agree = sum(1 for c in comparisons if c.agreement)
    fep = sum(1 for c in comparisons if c.false_early_pass)
    fef = sum(1 for c in comparisons if c.false_early_fail)
    early = sum(1 for c in comparisons if c.early_exit)
    escalated = sum(1 for c in comparisons if c.stages > 1)
    cost = sum(c.actual_cost_units for c in comparisons)
    always = ALWAYS_EXPENSIVE_COST_UNITS * n
    return {
        "mix": mix_name,
        "mode": mode.value,
        "pressure": pressure.value,
        "jobs": n,
        "decision_agreement": round(agree / n, 4) if n else 0.0,
        "false_early_pass": fep,
        "false_early_fail": fef,
        "early_exit_rate": round(early / n, 4) if n else 0.0,
        "escalation_rate": round(escalated / n, 4) if n else 0.0,
        "actual_cost_units": cost,
        "always_expensive_cost_units": always,
        "compute_savings": round(1.0 - cost / always, 4) if always else 0.0,
        "elapsed_seconds": round(elapsed, 3),
        "kind_counts": dict(Counter(k for k, _ in jobs)),
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
            "limitation": "in-process only; not cross-process",
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-path", default="docs/adaptive-evaluation-v3-results.json"
    )
    args = parser.parse_args()

    modes = [
        EvaluationMode.ALWAYS_EXPENSIVE,
        EvaluationMode.FIXED_PROGRESSIVE,
        EvaluationMode.RESOURCE_AWARE_ADAPTIVE,
    ]
    pressures = [PressureState.NORMAL, PressureState.PRESSURED, PressureState.DEGRADED]
    output: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "environment": env_snapshot(),
            "ground_truth": "WorkloadKind suffix _pass/_fail; score bands via ground_truth_for",
            "safe_early_exit": "confidence AND safe_to_terminate",
            "claim": (
                "Architecture validation under adversarial synthetic workloads — "
                "not AI quality claims"
            ),
        },
        "scenarios": {},
        "scorecard": {},
        "coalescing": {},
    }

    for mix in MIXES:
        for mode in modes:
            for pressure in pressures:
                # Always-expensive ignores pressure for cost; still run once per mix.
                if mode == EvaluationMode.ALWAYS_EXPENSIVE and pressure != PressureState.NORMAL:
                    continue
                key = f"{mode.value}__{mix}__{pressure.value}"
                print(f"scenario {key}", flush=True)
                result = run_mix(mix, mode, pressure)
                output["scenarios"][key] = result
                output["scorecard"][key] = {
                    "decision_agreement": result["decision_agreement"],
                    "false_early_pass": result["false_early_pass"],
                    "false_early_fail": result["false_early_fail"],
                    "compute_savings": result["compute_savings"],
                    "early_exit_rate": result["early_exit_rate"],
                    "escalation_rate": result["escalation_rate"],
                }

    output["coalescing"] = asyncio.run(coalescing_bench())
    output["metadata"]["completed_at"] = utc_iso()

    # Decision summary for keep/retune/simplify
    prog_keys = [
        k
        for k, v in output["scorecard"].items()
        if k.startswith("fixed_progressive__") and k.endswith("__normal")
    ]
    agreements = [output["scorecard"][k]["decision_agreement"] for k in prog_keys]
    savings = [output["scorecard"][k]["compute_savings"] for k in prog_keys]
    fep = sum(output["scorecard"][k]["false_early_pass"] for k in prog_keys)
    output["verdict_inputs"] = {
        "fixed_progressive_normal_min_agreement": min(agreements) if agreements else None,
        "fixed_progressive_normal_mean_savings": round(sum(savings) / len(savings), 4)
        if savings
        else None,
        "fixed_progressive_normal_false_early_pass_total": fep,
    }

    path = ROOT / args.results_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()

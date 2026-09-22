# Resource pressure experiment

## Hypothesis

Under a mixed LIGHT/MEDIUM/HEAVY evaluation load plus interactive reads:

- **A** (no controller, static max=2) completes work but may not protect interactive traffic under heavier host pressure.
- **B** (runtime on, frozen min=max=2) behaves like static limits with observability.
- **C** (adaptive min=1 max=3, `resource_aware`) should keep interactive latency competitive while still finishing the backlog.

Constrained objective:

```text
maximize completed evaluations
subject to interactive errors = 0 and interactive p95 remaining healthy
```

## Workload

- 12 LIGHT + 8 MEDIUM + 4 HEAVY evaluations (24 total)
- 40 interactive challenge reads interleaved after enqueue
- Embedded PostgreSQL, single API process, one evaluation worker loop
- Script: `scripts/resource_pressure_experiment.py`
- Results: `docs/resource-pressure-experiment-results.json`

## Results (this laptop run)

| Mode | Completed | Eval RPS | Interactive p95 | Errors | CPU% | RSS MB | Adaptive max | Useful-work proxy |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A no controller | 24 | 2.63 | 107 ms | 0 | 94.5 | 88.7 | n/a (static 2) | 22.4 |
| B static + runtime | 24 | 2.90 | 116 ms | 0 | 96.2 | 89.2 | 2 | 20.8 |
| C adaptive | 24 | 2.98 | 109 ms | 0 | 99.0 | 89.4 | **3** | 22.1 |

All modes: **submissions_durable = 24**, **admission_holds = 0**, queue drained.

## Interpretation

1. **Correctness:** No lost submissions; all evaluations completed in every mode.
2. **Interactive:** p95 stayed ~100–120 ms across A/B/C on this mix — no regression from enabling the controller.
3. **Adaptive behavior:** Mode C raised `adaptive_max_workers` to 3 when backlog + low classified pressure allowed it, then finished slightly faster (2.98 vs 2.63 eval/s).
4. **What did NOT dramatically improve:** On this modest 24-job mix, interactive latency was already healthy; the controller’s value is **policy + headroom under scarcity**, not a free throughput miracle.
5. **Default policy:** Keep `bounded_light_bypass` as the claim default; keep resource runtime **enabled** for concurrency adaptation. Do not make `resource_aware` the default claim policy until a heavier adversarial mix shows a clear interactive win.

## Scorecard fields

Interactive SLO · Evaluation throughput · Queue remaining · CPU · Memory · Admission holds · Starvation (bypass bounds) · Errors · Recovery (drain after burst) · Durability.

## Next measurement that would justify more machinery

- Interactive p95 climbs while adaptive concurrency is already at `min` → strengthen bulkheads / process isolation.
- HEAVY starves beyond documented bypass → revise scheduling, not add Redis.
- Identical expensive work dominates CPU → promote coalescing beyond the unit experiment.

# Resource budgets

## Question every engineer should answer

> What resource are we protecting, and what happens when we run out?

| Budget | Protects | On exhaustion |
|---|---|---|
| `evaluation_min_workers` … `evaluation_max_workers` | Expensive-plane concurrency | Adaptive controller cannot leave this range; claim refuses when RUNNING ≥ effective max |
| `evaluation_max_concurrent_heavy` | HEAVY slot | HEAVY waits (LIGHT may bypass within limit) |
| Soft/hard queue depths | Backlog honesty + pressure | PRESSURED / DEGRADED states; submissions still accepted |
| CPU low/high % | Host compute for interactive plane | Shrink expensive concurrency |
| Memory soft/hard MB | Process RSS | Hold new expensive starts when hard + in-flight |
| Adjust cooldown | Stability | Prevents oscillation |

## Defaults (laptop assumptions)

Configured in `Settings` / env:

| Knob | Default | Assumption |
|---|---:|---|
| `evaluation_min_workers` | 1 | Always keep at least one expensive slot when admitted |
| `evaluation_max_workers` | 1 | Prefer interactive headroom until operator raises the ceiling |
| `resource_cpu_low_percent` | 35 | Headroom to *increase* concurrency |
| `resource_cpu_high_percent` | 75 | Enter DEGRADED / decrease |
| `resource_memory_soft_mb` | 256 | Soft pressure |
| `resource_memory_hard_mb` | 400 | Hard hold |
| `resource_adjust_cooldown_seconds` | 2 | Slow adjustments |
| `resource_aware_runtime_enabled` | true | Opt-out with `false` for A/B experiments |

These are **not** production SLOs. Raise `evaluation_max_workers` only when measured capacity and interactive latency allow it (see isolated load profile: ~40 RPS interactive with workers isolated).

## Planes

```text
INTERACTIVE  — HTTP reads, submission accept, status, organizer ops
EXPENSIVE    — evaluation (future: LLM, ingestion, code exec)
```

Budgets apply to the expensive plane. Interactive acceptance is never rejected for capacity.

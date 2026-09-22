# Resource scheduling

## Policies

| Policy | Behavior | Default? |
|---|---|---|
| `fifo` | Oldest queued wins; HEAVY may block behind its concurrency slot | no |
| `bounded_light_bypass` | When oldest HEAVY is blocked, at most N consecutive LIGHT successors may bypass | **yes** |
| `resource_aware` | Same claim mechanics as bounded LIGHT bypass; under DEGRADED sets `max_concurrent_heavy=0` so HEAVY does not start while LIGHT can still bypass | experiment |

## What we measured previously

Bounded LIGHT bypass improved LIGHT latency without unbounded HEAVY starvation
(`docs/evaluation-scheduling.md`).

## Resource-aware addition (v1)

Does **not** replace FIFO fairness with a priority jungle.

Under `PressureState.DEGRADED`:

1. Adaptive concurrency moves toward `min_evaluation_concurrency`
2. New HEAVY starts are refused (`max_concurrent_heavy=0`)
3. Bounded LIGHT bypass may still advance LIGHT work
4. MEDIUM/HEAVY remain durable QUEUED — never deleted

## Cost classes

`WorkloadClass` LIGHT / MEDIUM / HEAVY map to bounded synthetic profiles in
`application/evaluator.py`. Future expensive work should declare an
`ExpensiveWorkSpec` (`runtime/expensive_work.py`) with the same cost vocabulary.

## Promotion rule

Keep `resource_aware` as default **only** if the pressure experiment scorecard
shows better interactive p95 × completed-work trade-off than
`bounded_light_bypass` alone. Until then, default remains `bounded_light_bypass`
with the resource runtime still adapting concurrency.

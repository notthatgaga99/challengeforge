# Evaluation cost model (Adaptive Evaluation v2)

## Stage cost units

| Stage | Workload class | Cost units | Est. duration |
|---|---|---:|---:|
| cheap | light | 1 | ~20 ms |
| medium | medium | 3 | ~80 ms |
| heavy | heavy | 9 | ~200 ms |

Always-expensive baseline per job: **13** cost units.

## Compute savings

```text
compute_savings = 1 - actual_cost_units / (13 * completed_jobs)
```

`actual_cost_units` is the sum of `cost_units` for stages that executed
(including deferred resume). Documented in `result_metadata.progressive`
(internal only — not participant API).

## Quality note

Scores and confidence are **synthetic deterministic functions** of submission
metadata / id. They do not measure real solution quality.

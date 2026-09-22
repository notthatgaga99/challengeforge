# Adaptive Evaluation v2 — Results

**Claim (accurate):** We built a resource-aware progressive evaluation architecture
and demonstrated **deterministic compute savings** under synthetic workloads.

**Not claimed:** The evaluator is intelligent / LLM-quality.

Raw data: `docs/adaptive-evaluation-results.json`  
Harness: `scripts/adaptive_evaluation_experiment.py`

## Modes

| Mode | Behavior |
|---|---|
| A `always_expensive` | CHEAP→MEDIUM→HEAVY always (13 cost units) |
| B `fixed_progressive` | Early exit on confident cheap; escalate on uncertain |
| C `resource_aware_adaptive` | B + may defer HEAVY under pressure |

## Scorecard (primary mixes)

| Mode × mix | Compute savings | Early-exit rate | Escalation rate | Inter p95 | Eval/s | Done |
|---|---:|---:|---:|---:|---:|---:|
| A · all confident | 0% | 0% | 100% | 54 ms | 1.10 | 12/12 |
| B · all confident | **92%** | 100% | 0% | 107 ms | 4.71 | 12/12 |
| C · all confident | **92%** | 100% | 0% | 103 ms | 4.04 | 12/12 |
| A · 50/50 | 0% | 0% | 100% | 604 ms | 1.39 | 12/12 |
| B · 50/50 | **46%** | 50% | 50% | 96 ms | 2.15 | 12/12 |
| C · 50/50 | **46%** | 50% | 50% | 98 ms | 2.01 | 12/12 |
| A · 80/20 | 0% | 0% | 100% | 549 ms | 1.35 | 12/12 |
| B · 80/20 | **77%** | 83% | 17% | 99 ms | 3.53 | 12/12 |
| C · 80/20 | **77%** | 83% | 17% | 219 ms | 1.10 | 12/12 |

`compute_savings = 1 - actual_cost_units / (13 × jobs)`.

## Interpretation

1. **Progressive evaluation works:** when cheap confidence is common, B/C avoid
   ~77–92% of synthetic expensive work vs always-expensive.
2. **Interactive latency:** progressive modes often *lower* interactive p95 under
   the same host because workers finish sooner (less HEAVY wall time).
3. **Adaptive vs fixed:** on these mixes savings matched; C’s deferral path still
   drained (queued_remaining=0) after pressure eased. C is valuable as a *safety
   valve*, not as a throughput miracle on light mixes.
4. **Correctness:** all submissions durable; all evaluations completed in matrix.

## Coalescing

In-process coalescer unit test: 20 waiters → **1** computation (`tests/test_resource_aware_runtime.py`).
Limitation: process-local only — not a global multi-worker dedupe. Documented; no Redis.

## Fairness

Bounded LIGHT bypass unchanged. Deferred HEAVY jobs remain QUEUED and were completed
after pressure hint cleared. No indefinite postpone observed in this matrix.

## What did NOT improve

Adaptive mode did not beat fixed progressive on savings for these mixes. Keep
`fixed_progressive` as the clear efficiency win; keep `resource_aware_adaptive`
when pressure deferral of HEAVY is desired.

## Default

Production default remains **`legacy`** until operators opt into progressive modes.
Promote progressive only after broader adversarial fairness runs.

## Next justified change

- Real LLM/RAG stages behind `ExpensiveWorkSpec` once product needs them  
- Stronger process isolation if interactive p95 collapses at min concurrency  
- Global coalescing only if multi-process duplicate expensive work is measured  

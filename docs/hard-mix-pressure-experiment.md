# Hard-Mix Resource Pressure Experiment — Design

**Status:** Measurement only (no new infrastructure)  
**Question:** Can ChallengeForge process difficult/adversarial evaluations under
real HTTP pressure while preserving interactive responsiveness, correctness,
bounded resources, and the safe early-exit contract?

**Harness:** `scripts/hard_mix_pressure_experiment.py`  
**Results:** `docs/hard-mix-pressure-results.md` (+ JSON)

---

## What this is / is not

| Is | Is not |
|---|---|
| Isolated API OS process + separate load-gen + worker process | Same-process load-gen (invalidates capacity) |
| v3 `WorkloadKind` + ground-truth decision agreement under HTTP | An LLM/RAG quality claim |
| Offered vs achieved RPS distinguished | A manufactured “API capacity” number |
| Honest collapse of progressive savings on hard mixes | Optimizing the benchmark for progressive wins |

Do **not** add Redis, Kafka, Kubernetes, replicas, external queues, or a workflow engine.

---

## Planes and metrics (do not conflate)

```text
offered HTTP load     → target RPS from the load generator
achieved HTTP throughput → completed interactive requests / wall time
evaluation arrival rate → submissions accepted into QUEUED / wall time
evaluation completion rate → SUCCEEDED+FAILED / wall time
```

Interactive plane health is the constrained objective. Expensive evaluation may
queue or slow down; submissions must remain durable.

---

## Interactive workload

Same mix as the isolated interactive profile:

- challenge read
- evaluation status
- participant history
- submission creation

Load generator runs in the **parent** process. API and evaluation workers are
**separate** OS processes. All share one laptop host (CPU/memory contention is
real and reported separately by process).

Rate sweep starts at **10 / 20 / 40** RPS. Higher rates (60 / 80 / 100) run
**only** while saturation criteria remain clean. If the load generator or host
saturates, record the reason and stop — do not invent capacity.

Saturation stop (same spirit as isolated profile):

- timeout rate ≥ 5%
- error rate ≥ 5%
- achieved < 70% of offered (offered ≥ 10)
- client or server p95 ≥ 2000 ms

---

## Evaluation workload (deterministic, seeded)

Kinds from Adaptive Evaluation v3 (`WorkloadKind`):

| Kind | Cheap early-exit? | Expected stages |
|---|---|---|
| EASY_PASS / EASY_FAIL | Yes (safe) | 1 |
| AMBIGUOUS_* | No at cheap; yes at medium | 2 |
| HARD_* | No until heavy | 3 |
| ADVERSARIAL_* | Cheap may be confident-**wrong** but `safe_to_terminate=false` | 3 |

### Mixes

| Mix | Distribution (approx.) |
|---|---|
| mostly_easy | 80% EASY, 20% AMBIGUOUS |
| balanced | 40% EASY, 30% AMBIGUOUS, 30% HARD |
| difficult | 20% EASY, 40% AMBIGUOUS, 40% HARD |
| adversarial | mostly ADVERSARIAL + HARD |
| heavy_dominated | 10% EASY, 20% AMBIGUOUS, 70% HARD |
| hard_70 | 70% HARD/ADVERSARIAL, 30% EASY/AMBIGUOUS |
| hard_90 | 90% HARD/ADVERSARIAL, 10% EASY |

Seeded with a fixed RNG seed per scenario. PASS/FAIL halves are balanced unless
noted. Initial `workload_class` mirrors cost hint (EASY→LIGHT, AMBIGUOUS→MEDIUM,
HARD/ADVERSARIAL→HEAVY); progressive modes still escalate stages via the plan.

---

## Policies (evaluation modes)

| Label | `evaluation_mode` | Intent |
|---|---|---|
| A | `legacy` | Control — hash-based score; **not** bound to v3 ground truth |
| B | `always_expensive` | Full CHEAP→MEDIUM→HEAVY always |
| C | `fixed_progressive` | Safe early-exit when evidence allows |
| D | `resource_aware_adaptive` | Progressive + pressure-aware HEAVY deferral |

Per-submission opt-in via metadata (`evaluation_mode`). Default product mode
remains `legacy`. Worker resource budgets stay comparable across A–D so the
comparison is honest.

**Quality note:** Decision agreement vs v3 ground truth applies to B/C/D.
Mode A is reported for interactive/resources only; agreement is `null`.

---

## Evaluation pressure levels

| Level | Behavior |
|---|---|
| low | Small fixed batch enqueued before interactive window |
| medium | Sustained arrival during interactive window |
| high | Arrival exceeds evaluation capacity (queue grows) |
| burst | Short flood, then stop; measure drain/recovery |

The key combined case: **steady interactive traffic + rising evaluation pressure**.

---

## Resource budgets (laptop defaults for this harness)

| Setting | Value |
|---|---|
| API db pool | 8 + overflow 4 |
| evaluation_min_workers | 1 |
| evaluation_max_workers | 2 |
| evaluation_max_concurrent_heavy | 1 |
| light_bypass_limit | 2 |
| scheduling_policy | `bounded_light_bypass` |
| resource_aware_runtime_enabled | true |
| Max evals / scenario | 24 (safety) |
| Interactive duration | ≤ 12 s (safety) |

---

## Quality × cost under HTTP

For each completed progressive evaluation:

```text
ground_truth = WorkloadKind suffix (_pass / _fail)
decision     = score >= 60
agreement    = decision == ground_truth
```

Also report: false early-pass/fail, early-exit rate, escalation rate,
heavy-stage invocation rate, actual cost units vs always-expensive baseline.

A correct hard-mix result may show **near-zero compute savings** because safe
early exit refuses to terminate. That is a valid outcome.

---

## Constrained objective (Pareto — no single score)

```text
interactive p95  ×  evaluation throughput  ×  queue wait
×  decision agreement  ×  compute consumed
```

Questions:

- **A** Does progressive reduce expensive computation?
- **B** Does it preserve decision correctness?
- **C** Does resource-aware scheduling protect interactive traffic?
- **D** Does adaptive progressive improve anything vs fixed progressive?
- **E** Under which workload does each policy stop being useful?

---

## Failure / recovery / coalescing

Failure-between-stages and stale RUNNING recovery are covered by existing
pytest + targeted hard-mix tests (not every cell of the HTTP matrix).

Coalescing: exercise in-process coalescer if reachable; **document** that it is
not global across API processes. Prefer documenting the limitation over adding Redis.

---

## Decision gate (after results)

| Outcome | When |
|---|---|
| KEEP | Meaningful value under pressure + high agreement + bounded resources |
| KEEP FIXED, DROP ADAPTIVE | Progressive useful; adaptive adds little |
| RETUNE | Thresholds/concurrency poorly calibrated |
| SIMPLIFY | Complexity does not improve the constrained objective |

Evidence beats sunk cost. Real LLM/RAG work is **not** justified until this
experiment’s decision gate is written from measured results.

# Adaptive Evaluation v2 — Design

**Status:** Accepted for synthetic/progressive architecture experiment  
**Date:** 2026-09-22  
**Not a claim of AI quality.** This is a resource-aware *progressive computation* design.

---

## Problem

Today every evaluation spends a fixed synthetic cost once claimed. Under scarcity we want:

```text
spend expensive compute only when cheap evidence is insufficient
while protecting interactive latency, fairness bounds, and correctness
```

Core question:

> When compute is scarce, can ChallengeForge spend less compute while still producing useful, correct evaluations and protecting the interactive UX?

---

## Alternatives (decision space)

| # | Approach | Saves | Quality risk | Complexity | Fairness | Now? |
|---|---|---|---|---|---|---|
| 1 | Always-expensive | nothing | lowest miss risk | low | FIFO-friendly | **Baseline A** |
| 2 | Fixed multi-stage | nothing if always all stages | — | medium | — | subset of A |
| 3 | Confidence-based escalation | expensive stages | wrong early exit if confidence lies | medium | OK if durable | **Yes — core** |
| 4 | Cost-aware scheduling | expensive starts under pressure | deferred quality | medium | need age bounds | **Yes — light** |
| 5 | Deadline-aware | time-to-result | can starve non-deadline | medium | trade-off | **Data model + experiment only** |
| 6 | Priority scheduling | — | starvation | medium | hard | No (bypass exists) |
| 7 | Utility optimizer | theoretical | opaque | high | opaque | **No** |
| 8 | Early exit | CPU | false confident | low–medium | OK | **Yes** |
| 9 | Approximate eval | CPU | wrong answers | high | — | No (synthetic confidence only) |
| 10 | Progressive eval | staged cost | partial work | medium | OK with durability | **Yes** |
| 11 | Model routing | LLM $ | wrong model | high | — | Future |
| 12 | Human escalation | compute | latency | high | — | Future |

### Chosen smallest coherent mechanism

```text
CHEAP stage → Confidence{PASS|FAIL|UNCERTAIN}
    ├─ confident → early exit (finish)
    └─ uncertain → escalate MEDIUM → (maybe) HEAVY
         └─ under DEGRADED/PRESSURED: may defer escalation (requeue durable)
```

Modes for measurement:

- **A always_expensive** — always CHEAP→MEDIUM→HEAVY  
- **B fixed_progressive** — escalate only on UNCERTAIN  
- **C resource_aware_adaptive** — B + pressure-aware deferral of expensive stages  

Default production path remains **`legacy`** (single-shot evaluator) until evidence promotes progressive.

---

## Stage progress durability (Phase 12)

| Option | Pros | Cons | Decision |
|---|---|---|---|
| **A. One evaluation row + stage fields** | Simple, one job identity, fits SKIP LOCKED | Less audit granularity | **Chosen** |
| B. Separate stage rows | Fine-grained | Extra joins, almost a workflow | Later if needed |
| C. Workflow engine | Generality | Heavy, unjustified | **No** |

Durable fields: `current_stage`, `evaluation_mode`, optional `deadline_at`, plus `result_metadata.progressive` accounting.

Crash after partial stages: stale RUNNING recovery requeues; `current_stage` preserved so work resumes without inventing a completed result.

---

## Confidence semantics (synthetic)

Cheap stage maps **explicit submission metadata scenarios** to confidence — deterministic, not ML:

| `metadata.adaptive_scenario` | Cheap confidence |
|---|---|
| `pass_confident` | PASS_CONFIDENT |
| `fail_confident` | FAIL_CONFIDENT |
| `uncertain` | UNCERTAIN |
| `requires_expensive` | UNCERTAIN (forces full chain in progressive) |
| (absent) | derived deterministically from submission id hash bucket |

**Do not claim intelligence.** We test architecture of progressive computation.

---

## Cost / savings metric

```text
estimated_cost_units(stage) ∈ {1, 3, 9}   # CHEAP, MEDIUM, HEAVY
actual_cost_units = sum(stages_completed)

compute_savings =
  1 - adaptive_cost_units / always_expensive_cost_units

where always_expensive_cost_units = 1+3+9 = 13 per job
```

Report savings only with this definition.

---

## Fairness

- Existing bounded LIGHT bypass unchanged for claim order.  
- Deferred escalations remain QUEUED (never dropped).  
- Oldest deferred expensive work must not be skipped forever: under NORMAL pressure, escalate; optional deadline raises urgency in experiments.  
- Measure max/p95 wait and oldest age in the fairness scenario.

---

## Non-goals

Redis, workflow engines, real LLM/RAG, claiming “smarter evaluation,” rejecting submissions for capacity.

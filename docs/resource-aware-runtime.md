# Resource-Aware Runtime v1 — Design

**Status:** Accepted for implementation (laptop / portfolio monolith)  
**Date:** 2026-09-22  
**Evidence base:** interactive isolation profile (`docs/interactive-isolated-load-profile.md`),
evaluation capacity, FIFO + bounded LIGHT bypass.

---

## Story we are telling

> This system was designed around **explicit resource constraints** rather than
> assuming infinite CPU, memory, database connections, or future LLM capacity.

Interactive traffic must stay responsive. Expensive work (evaluation today;
ingestion / RAG / LLM later) may slow down, queue, or shrink concurrency — but
must never erase durable user actions or consume unbounded resources.

---

## Alternatives considered (Phase 0)

| # | Approach | Problem solved | Resources | Complexity | Failure modes | Justified NOW? | Later if… |
|---|---|---|---|---|---|---|---|
| 1 | **Static worker limits** | Cap expensive concurrency | CPU/mem/DB | Low | Under-utilizes quiet periods; overloads if mis-set | **Yes — keep as hard ceiling** | Always keep as safety bound |
| 2 | **Dynamic/adaptive concurrency** | React to pressure / backlog | CPU/mem | Medium | Oscillation, thrash | **Yes — v1 feedback controller** | Retune gains with more host types |
| 3 | **Admission control** | Stop starting expensive work under pressure | CPU | Medium | If misapplied to submissions → UX disaster | **Yes — expensive plane only** | Expand classes (LLM slots) |
| 4 | **Priority scheduling** | Prefer important jobs | Fairness trade-offs | Medium | Starvation | Partial (bounded LIGHT bypass exists) | Only with measured fairness win |
| 5 | **Resource-aware scheduling** | Match job cost to headroom | CPU/mem | Medium–high | Complexity, unexpected order | **Experiment only in v1** | Promote if scorecard improves |
| 6 | **Bulkheads** | Isolate interactive vs expensive | Isolation | Medium | Weak if same process | **Logical budgets now** | Process isolation if measured contention |
| 7 | **Backpressure** | Signal overload honestly | UX | Low | Lying estimates | **Yes — extend backlog + pressure** | — |
| 8 | **Graceful degradation** | Explicit behavior under scarcity | Policy | Medium | Cosmetics without teeth | **Yes — NORMAL/PRESSURED/DEGRADED** | Add more states only if needed |
| 9 | **Request coalescing** | Dedup identical expensive work | CPU | Medium | Cancellation / correctness | **Optional experiment** | LLM/RAG fan-in |
| 10 | **Caching** | Avoid repeat work | Memory/consistency | Medium | Stale answers | **No** | Proven hot identical reads/evals |
| 11 | **External queues** | Scale claim/poll | Ops | High | Dual-write, new failure domain | **No** (ADR 0004) | Polling/conn pressure dominates |
| 12 | **Separate services** | Strong isolation / scale | Ops | Very high | Distributed complexity | **No** | Single process proven ceiling |

### Chosen coherent architecture (smallest)

```text
Interactive plane                 Expensive plane
─────────────────                 ────────────────
HTTP reads/writes                 Evaluation claim → execute
Submission ALWAYS durable         Bounded by ResourceBudget
Honest backlog / pressure API     Adaptive concurrency
                                  Admission may delay *start*
                                  NEVER rejects submission
```

Components:

1. **ResourceBudget** — configurable caps and thresholds  
2. **ResourceObserver** — CPU / RSS / queue / running samples  
3. **PressureState** — `normal | pressured | degraded`  
4. **ExpensiveAdmission** — gate on *claim*, not submit  
5. **AdaptiveConcurrencyController** — slow, bounded worker slots  
6. **Existing scheduler** — FIFO + bounded LIGHT bypass (default)  
7. **Optional** resource-aware claim filter under DEGRADED  
8. **ExpensiveWorkSpec** interface — future LLM/RAG hooks (no implementations)

---

## Non-goals (v1)

- Redis / Kafka / Celery / K8s  
- ML-based cost prediction  
- Rejecting valid submissions for capacity  
- Fake scores or silent skip of evaluation  
- Multi-region autoscaling  

---

## Control loop (precise)

Every worker poll (after recover_stale):

```text
observe(cpu%, rss_mb, queued, running, interactive_p95_hint?)
    → classify PressureState
    → AdaptiveConcurrencyController.adjust(effective_max_workers)
    → ExpensiveAdmission.may_start_evaluation(state, budgets)
    → if admitted: claim_next(max_workers=effective_max_workers, …)
    → else: sleep(poll_interval)  # work remains QUEUED
```

Adjustment rules (deterministic):

| Condition | Action |
|---|---|
| `degraded` OR cpu ≥ high OR rss ≥ high | decrease concurrency by 1 (floor = min) |
| `pressured` | hold or decrease if above min+1 |
| `normal` AND queued > 0 AND cpu ≤ low AND rss ≤ low | increase by 1 (ceil = max) |
| else | hold |
| Cooldown | no change more than once per `adjust_cooldown_seconds` |

Interactive protection: when `degraded`, effective concurrency collapses toward `min_evaluation_concurrency` and HEAVY starts are refused by admission (queued jobs stay durable).

---

## Success metric (constrained)

```text
maximize useful completed evaluations
subject to:
  interactive experience remains healthy (no submission rejection;
    prefer low interactive latency under mixed load)
  memory < budget
  CPU pressure does not stay in degraded without shrinking expensive plane
  DB pool not exhausted by worker claims
  no starvation beyond documented LIGHT-bypass bounds
  no correctness violations
```

See `docs/resource-pressure-experiment.md` for the scorecard.

# ADR 0005: Resource-Aware Runtime for the expensive plane

## Status

Accepted.

## Context

Isolated load generation showed ~40 RPS interactive capacity on a laptop while
evaluation workers compete for the same host. Static `evaluation_max_workers=1`
protects interactive traffic but cannot react to quiet backlog vs pressure.
Adding Redis/K8s would not address the measured problem (host contention /
expensive-plane greed).

## Decision

Implement **Resource-Aware Runtime v1** inside the modular monolith:

- explicit `ResourceBudget`
- `PressureState` = normal | pressured | degraded
- admission on evaluation *start* only (never on submission accept)
- adaptive concurrency within [min, max] with cooldown
- persist pressure + adaptive max on `evaluation_scheduler_state`
- keep PostgreSQL queue + bounded LIGHT bypass

## Alternatives

1. Always static workers — simple, leaves backlog headroom unused  
2. External autoscaler / K8s HPA — infra without evidence  
3. Reject submissions under load — violates product durability  
4. Separate eval microservice now — premature distribution  

## Trade-offs

**+** Inspectable policy; laptop-safe; prepares ExpensiveWorkSpec for LLM/RAG  
**−** Process-local CPU samples imperfect under multi-worker; needs host-noise awareness  

## Evidence

- Design: `docs/resource-aware-runtime.md`  
- Budgets: `docs/resource-budget.md`  
- Pressure experiment: `docs/resource-pressure-experiment.md`  

## Reconsideration triggers

- Interactive p95 still collapses while adaptive concurrency is already at min  
- DB pool wait dominates (then pool/query work)  
- Proven need for process-level bulkheads after logical budgets fail  

## Related

ADR 0003 (async evaluation), ADR 0004 (keep Postgres queue)

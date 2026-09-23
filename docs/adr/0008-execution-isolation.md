# ADR 0008: Execution isolation + evaluation integration (synthetic)

## Status

Accepted:

1. **KEEP SUBPROCESS** for the synthetic executor prototype  
   (`docs/execution-isolation-results.json`)
2. **KEEP EXECUTION INTEGRATION** for opt-in durable orchestration  
   (`docs/execution-evaluation-integration-results.json`)

Participant code execution remains **forbidden**. Hard network/cgroup isolation
is **not** claimed. Product default evaluation mode remains `legacy`.

## Context

ChallengeForge can admit and bound expensive synthetic evaluation work. The
process executor proved timeout/tree/output/workspace containment in isolation.
The next question was whether the durable evaluation pipeline can orchestrate
that executor without crossing the trust boundary.

## Decision

1. Keep subprocess + tree cleanup as the execution plane for trusted workloads.
2. Add opt-in `evaluation_mode=synthetic_execution` (migration `0010`).
3. Separate **execution evidence** (`ProcessExecutor`) from **evaluation
   disposition** (worker → SUCCEEDED/FAILED/requeue).
4. Allowlist workloads via `metadata.execution_workload` only — never arbitrary
   source.
5. Label budgets ENFORCED / OBSERVED / NOT ENFORCED explicitly.
6. Do **not** wire participant uploads; do **not** add Docker/K8s/Redis/LLM.

## Alternatives

| Option | Why deferred / rejected for now |
|---|---|
| In-process | No isolation for hostile code |
| Containers first | Ops/startup cost before proving subprocess orchestration |
| MicroVM / remote executor | Unjustified without multi-tenant hostile corpus |
| New durable execution statuses | Unnecessary — RUNNING covers execute |
| Redis/K8s/LLM | Explicit non-goals |

## Evidence

- Design: `docs/execution-isolation.md`, `docs/execution-evaluation-integration.md`
- Tests: `tests/test_execution_isolation.py`, `tests/test_synthetic_execution.py`
- Harnesses: `scripts/execution_isolation_experiment.py`,
  `scripts/execution_evaluation_integration_experiment.py`

## Trade-offs

**+** Durable orchestration; clear outcome taxonomy; opt-in; no fashion infra  
**−** Soft FS containment; host network not denied; worker-death orphan reaping
is OS-dependent; interactive coupling still laptop-noisy

## Reconsideration

Move to containers / stronger isolation if:

- process leaks under orchestration, or
- mount/network deny is required, or
- public multi-tenant hostile code is in scope.

## Related

ADR 0003 (async evaluation boundary), ADR 0005 (resource-aware runtime),
evaluation pipeline docs.

# ADR 0008: Execution isolation for untrusted work (synthetic prototype)

## Status

Accepted for **design + synthetic prototype**. Decision gate: **KEEP SUBPROCESS**
(see `docs/execution-isolation-results.json`). Participant code execution remains
**forbidden**. Hard network/cgroup isolation is **not** claimed.

## Context

ChallengeForge can admit and bound expensive *synthetic* evaluation work, but
evaluation does not run participant artifacts. Before LLMs or real code judging,
we need an explicit **execution plane** with isolation and resource governance
treated as separate concerns.

## Decision

1. Document the threat model, boundary, alternatives, lifecycle, cancellation,
   filesystem/network/secrets models (`docs/execution-isolation.md`).
2. Implement a **subprocess + process-tree cleanup** prototype for deterministic
   synthetic workloads only (`challengeforge.execution`).
3. Enforce wall timeout, output caps, scrubbed child env, workspace cleanup.
4. Do **not** claim hard network/cgroup isolation on this Windows/laptop prototype.
5. Do **not** wire the executor into submission evaluation yet.
6. Fix experiment import bootstrap (`cf_experiment_paths`, `scripts` on pytest path).

## Alternatives

| Option | Why deferred / rejected for now |
|---|---|
| In-process | No isolation for hostile code |
| Containers first | Ops/startup cost before proving subprocess limits |
| MicroVM / remote executor | Unjustified without multi-tenant hostile corpus |
| Redis/K8s/LLM | Explicit non-goals |

## Evidence

- Design: `docs/execution-isolation.md`
- Tests: `tests/test_execution_isolation.py`
- Harness: `scripts/execution_isolation_experiment.py`
- Results: `docs/execution-isolation-results.json`

## Trade-offs

**+** Measurable lifecycle; tree cleanup; curriculum clarity; no fashion infra  
**−** Soft FS containment; host network not denied; OS-specific hard limits incomplete

## Reconsideration

Move to containers or stronger isolation if:

- need mount/network deny subprocess cannot provide, or
- invariants fail under capacity, or
- public multi-tenant hostile code is in scope.

## Related

ADR 0003 (async evaluation boundary), ADR 0005 (resource-aware runtime),
evaluation pipeline docs.

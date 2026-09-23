# ADR 0008: Execution isolation, integration, ownership, and resource governance

## Status

Accepted:

1. **KEEP SUBPROCESS** — synthetic executor prototype  
2. **KEEP EXECUTION INTEGRATION** — opt-in durable `synthetic_execution`  
3. **KEEP + OS OWNERSHIP** — Windows Job Object `KILL_ON_JOB_CLOSE` + recovery
   containment before requeue  
4. **KEEP + SELECTIVE RESOURCE LIMITS** — Job Object process-count / job-memory /
   CPU user-time / CPU-rate throttle where available; wall/output/workspace
   app-enforced; network/FS/cgroup **not** claimed  

Participant code remains **forbidden**. Default mode remains `legacy`.

## Context

Ownership answered who controls the process tree when a worker dies. Resource
governance asks which host resources an execution may consume and what happens
when budgets are exceeded — without pretending the platform is a sandbox.

## Decision

1. Keep ownership via KillOnJobClose + contain-before-requeue.  
2. Extend Job Objects with selective limits: `ActiveProcessLimit`,
   `JobMemoryLimit`, `PerJobUserTimeLimit`, optional CPU rate hard cap.  
3. Keep wall time, stdout/stderr caps, and workspace byte caps in the executor.  
4. Classify exceedances as distinct outcomes (`PROCESS_LIMIT`, `MEMORY_LIMIT`,
   `CPU_LIMIT`, `WORKSPACE_LIMIT`, …) in metadata — no new durable status table.  
5. Deterministic resource violations are terminal (no retry).  
6. Do not add containers, supervisors, or an external execution service yet.

## Alternatives

| Option | Why deferred / partial |
|---|---|
| Watchdog + observation only | Insufficient process/memory ceilings on Windows |
| Full Job Object suite as “sandbox” | Still no net/FS jail; nesting caveats |
| Containers / remote executor | Stronger isolation ≠ required yet for trusted corpus |
| Blind requeue on limit | Would duplicate work / amplify resource use |

## Evidence

- `docs/execution-isolation.md`  
- `docs/execution-evaluation-integration.md`  
- `docs/execution-ownership-recovery.md`  
- `docs/execution-resource-governance.md`  
- `tests/test_execution_governance.py`  
- `scripts/execution_resource_governance_experiment.py`

## Trade-offs

**+** Honest ENFORCED vs OBSERVED vs NOT ENFORCED matrix; native OS levers; no new infra  
**−** Windows-centric hard limits; nested-job process-count headroom; workspace poll race;
CPU rate is throttle not isolation

## Reconsideration

Job Objects unavailable/ineffective · workspace overshoot unacceptable · need
net/FS deny for any participant code path · orphan/limit races in production.

## Related

ADR 0003, ADR 0005; ownership and evaluation pipeline docs.

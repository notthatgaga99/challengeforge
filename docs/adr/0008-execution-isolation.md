# ADR 0008: Execution isolation, integration, and ownership

## Status

Accepted:

1. **KEEP SUBPROCESS** — synthetic executor prototype  
2. **KEEP EXECUTION INTEGRATION** — opt-in durable `synthetic_execution`  
3. **KEEP + OS OWNERSHIP** — Windows Job Object `KILL_ON_JOB_CLOSE` + recovery
   containment before requeue  

Participant code remains **forbidden**. Network/cgroup/FS jail **not** claimed.
Default mode remains `legacy`.

## Context

After integrating ProcessExecutor with the evaluation queue, worker death could
leave Postgres `RUNNING` while an OS execution survived — enabling duplicate
execution on stale requeue.

## Decision

1. Assign each synthetic execution to a Windows Job Object with
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` when available.
2. Persist `execution_attempt` metadata (id, root_pid, workspace, ownership)
   at process start (`begin_execution_attempt`).
3. Change `recover_stale_running` to **contain** live attempts before requeue;
   if containment fails → fail with `orphan_suspected` (no second start).
4. Do not add a supervisor process or external executor service yet.
5. Do not add a new durable status enum; use metadata + FAILED reason.

## Alternatives

| Option | Why deferred |
|---|---|
| Worker-only ownership | Orphans on hard kill (measured) |
| Local supervisor | Extra failure domain without evidence yet |
| Containers / remote service | Ownership ≠ sandbox; unjustified for this gap |
| Blind requeue | Creates duplicate execution risk |

## Evidence

- `docs/execution-isolation.md`  
- `docs/execution-evaluation-integration.md`  
- `docs/execution-ownership-recovery.md`  
- `tests/test_execution_ownership.py`  
- `scripts/execution_ownership_experiment.py`

## Trade-offs

**+** Native OS lifecycle coupling; duplicate prevention; no new infra  
**−** Windows-centric hard guarantee; POSIX relies more on recovery kill;
quarantine may fail evaluations that could have been retried

## Reconsideration

Supervisor or remote executor if Job Objects unavailable / ineffective in the
deployment target, or if orphan_suspected becomes common.

## Related

ADR 0003, ADR 0005, evaluation pipeline docs.

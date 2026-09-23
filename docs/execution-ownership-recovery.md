# Execution Ownership & Recovery

**Status:** Measured; decision in `docs/execution-ownership-recovery-results.json`  
**ADR:** `docs/adr/0008-execution-isolation.md`  
**Harness:** `scripts/execution_ownership_experiment.py`  
**Code:** `challengeforge.execution.job_object`, `ownership`, ProcessExecutor OS ownership

---

## Problem

Two independent truths can diverge after worker death:

| Store | Claim |
|---|---|
| Postgres | `evaluation = RUNNING` |
| OS | `execution process = still alive` |

Stale recovery can requeue the evaluation while the original process still runs →
**duplicate execution** for one evaluation.

Invariant:

> At most one execution attempt for a given evaluation may actively consume
> execution resources.

---

## Ownership model

| Role | Owner |
|---|---|
| Evaluation identity / terminal status | Postgres + worker orchestrator |
| Claim lease | `worker_id` on RUNNING row |
| OS process tree | **Windows Job Object** (`KILL_ON_JOB_CLOSE`) when available; else PID tracking |
| Workspace | Execution attempt (unique temp dir per run) |
| Cleanup | Executor finally + Job close; recovery `contain_attempt` |
| Recovery | `recover_stale_running` (contain → then requeue/fail) |

```text
evaluation RUNNING
      │
      ▼
worker (holds Job Object handle)
      │
      ▼
ProcessExecutor → root PID assigned to Job
      │
      └── descendants (inherit job)
```

Worker hard death → last Job handle closes → OS terminates job members.

---

## Design space

| Strategy | Notes |
|---|---|
| A Worker owns child | Simple; orphans on hard kill |
| **B Job Object / process group** | Native; KillOnJobClose on Windows |
| C Local supervisor | Extra process; supervisor can die too |
| D External executor service | Not justified yet |
| E Containers | Isolation ≠ this ownership question |

---

## Failure matrix (summary)

| Failure | Desired |
|---|---|
| Normal / timeout | Collect + terminal |
| Worker hard kill | Job kills execution; DB recovered via stale path |
| Stale recovery | **Contain** prior attempt before requeue |
| Contain fails | `orphan_suspected` → FAILED (no duplicate) |
| Cleanup twice | Idempotent |

---

## Execution-attempt identity

Stored in `result_metadata.execution_attempt` (no new table):

```text
execution_attempt_id, evaluation_id, worker_id, workload,
root_pid, pgid, workspace, ownership
```

Persisted via `begin_execution_attempt` as soon as the process starts.

Workspaces remain unique per attempt (`cf-exec-*` temp dirs).

---

## Duplicate-execution prevention

`recover_stale_running`:

1. Load RUNNING rows past stale threshold  
2. If `execution_attempt.root_pid` alive → `contain_attempt`  
3. If still alive → **FAILED** with `orphan_suspected` (refuse requeue)  
4. Else requeue or abandon by attempt_count  

---

## Experiments

Harness: `scripts/execution_ownership_experiment.py`  
Results: `docs/execution-ownership-recovery-results.json`

### Worker hard-kill matrix (Windows)

| Scenario | Orphaned after kill | Alive after wait | Containment |
|---|---|---|---|
| **with Job Object** | no | no | n/a (OS killed with worker) |
| **without Job Object** | **yes** | **yes** | `contain_attempt` killed tree; `contained=true` |

Detection / death observation window ≈ **9.4 s** in the harness poll loop
(not a Job Object latency claim — the process is gone by the first successful
death observation after kill).

### Capacity microbench (LIGHT × 3)

| Ownership | mean wall ms | batch s |
|---|---|---|
| off | 980.73 | 8.558 |
| on (Job Object) | 860.19 | 8.252 |

No material capacity regression in this microbench (noise-dominated).

### Interactive plane (Phase 12)

Re-ran `execution_evaluation_integration_experiment.py --profile interactive`
(15 RPS target, 4s cells, 0/1/2 concurrent executions) after ownership:

| Cell | client p95 ms | achieved RPS |
|---|---|---|
| A_none | 3892 | 6.2 |
| B_one | 4589 | 5.7 |
| C_two | 4667 | 4.9 |

**Interpretation:** A_none is already degraded on this host run (available RAM was
low during ownership experiments). Ownership is off the API hot path; the
regression does **not** show an ownership-specific interactive bottleneck vs
execution pressure — all cells are similarly host-saturated. Prior SEI baseline
under healthier conditions was ~52 ms p95 @ ~15 RPS. See
`docs/execution-ownership-interactive-regression.json`.

### Duplicate-execution finding

Without Job Objects, an orphaned execution **survives** worker hard kill.
Recovery **must** contain by stored `root_pid` before requeue; if containment
fails → `orphan_suspected` / FAILED (never start a second attempt blindly).

---

## Decision

### KEEP + OS OWNERSHIP

Measured on Windows: without a Job Object, hard-killing the owning worker can
leave the synthetic execution alive; with `KILL_ON_JOB_CLOSE`, the execution
dies with the worker. Stale recovery contains by stored `root_pid` before
requeue, and refuses requeue if containment fails (`orphan_suspected`).

Participant uploads remain forbidden. Public sandbox still **not** claimed.

---

## Security checkpoint

Unchanged: **not** ready for public participant code. No network deny / cgroups /
FS jail / multi-tenant hostile claims.

## Limitations

- Job Objects are Windows-specific; POSIX uses process-group + recovery kill  
- Orphan quarantine is conservative (fail vs requeue)  
- Shared-laptop noise  

## Reconsideration

- Job Object assign fails in production containers → revisit supervisor  
- Need cross-host ownership → external execution service  
- Orphan_suspected rate high → strengthen containment  

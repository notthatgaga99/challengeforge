# Execution Resource Governance

**Status:** Measured — **KEEP + SELECTIVE RESOURCE LIMITS**  
**ADR:** `docs/adr/0008-execution-isolation.md`  
**Harness:** `scripts/execution_resource_governance_experiment.py`  
**Results:** `docs/execution-resource-governance-results.json`

This document is about **resource governance**, not ownership and not sandbox
security. Those are different guarantees:

| Concern | Question | Status |
|---|---|---|
| Ownership | Who owns the process tree if the worker dies? | KEEP + OS OWNERSHIP |
| Resource governance | What budgets bind CPU/memory/processes/IO/disk? | **This doc** |
| Security isolation | Network deny, FS jail, hostile multi-tenant? | **Not claimed** |

---

## Problem

Within the current subprocess + Windows Job Object ownership boundary, can one
execution exhaust the host (CPU, memory, process fan-out, output pipes,
workspace disk) — and can we bound that without pretending we have cgroups /
containers / a remote executor?

---

## Threat / resource model

| Resource | Classification (this host) | Mechanism |
|---|---|---|
| Wall time | **ENFORCED** | Executor poll → terminate |
| stdout / stderr bytes | **ENFORCED** | Stream caps |
| Process count | **ENFORCED** | Job `ActiveProcessLimit` + poller |
| Job commit memory | **ENFORCED** | Job `JobMemoryLimit` |
| Soft RSS watch | **ENFORCED** when `max_rss_mb` set | Poller (racey) |
| Peak RSS | **OBSERVED** | psutil |
| CPU user-time budget | **ENFORCED** | Job `PerJobUserTimeLimit` |
| CPU rate | **ENFORCED_THROTTLE** | Job CPU rate hard cap |
| CPU accounting | **OBSERVED** | Evidence only |
| Workspace bytes | **ENFORCED** (app; race window) | Directory walk poll |
| Process-tree cleanup | **ENFORCED** | Terminate + KillOnJobClose |
| Workspace cleanup | **ENFORCED** | rmtree (idempotent) |
| Network | **NOT ENFORCED** | — |
| cgroup CPU/memory | **NOT ENFORCED** | — |
| Filesystem jail | **NOT ENFORCED** | — |

Nested-job caveat: under IDEs/terminals the process may already be in a job;
`ActiveProcessLimit=N` may allow fewer than `N-1` children. Still a hard ceiling
(CreateProcess → WinError 1816).

---

## Design space

| Option | Guarantees | Complexity | Chosen? |
|---|---|---|---|
| A Watchdog + observation | Wall/output; RSS observe | Low | Baseline retained |
| **B Job Object resource limits** | Process/memory/CPU time/rate | Medium | **Selective yes** |
| C Containers / cgroups | Stronger isolation + limits | High | Not now |
| D External execution service | Hard multi-tenant boundary | High | Not justified yet |

---

## Explicit budgets (`ExecutionLimits`)

| Budget | Starts | Owner | On exceed | Retry? |
|---|---|---|---|---|
| `wall_timeout_seconds` | process start | executor | TIMEOUT | no |
| `max_stdout/stderr_bytes` | first byte | stream cap | OUTPUT_LIMIT | no |
| `max_process_count` | job assign | OS + poller | PROCESS_LIMIT | no |
| `max_job_memory_mb` | job assign | OS | MEMORY_LIMIT | no |
| `max_rss_mb` | first poll | poller | MEMORY_LIMIT | no |
| `max_cpu_seconds` | job assign | OS | CPU_LIMIT | no |
| `cpu_rate_percent` | job assign | OS | throttle (not terminal) | n/a |
| `max_workspace_bytes` | first walk | poller | WORKSPACE_LIMIT | no |
| `cleanup_deadline_seconds` | cleanup start | evidence | — | — |

Deterministic resource violations are **terminal** (no requeue).

---

## Outcome taxonomy

`SUCCESS`, `NONZERO_EXIT`, `TIMEOUT`, `OUTPUT_LIMIT`, `PROCESS_LIMIT`,
`MEMORY_LIMIT`, `CPU_LIMIT`, `WORKSPACE_LIMIT`, `START_FAILURE`,
`EXECUTOR_ERROR`, `CLEANUP_ERROR`, `ORPHAN_SUSPECTED`.

Mapped in `result_metadata` / executor status — **no new durable enum/table**.

---

## Evidence (this laptop)

From `execution-resource-governance-results.json`:

| Probe | Result |
|---|---|
| Process limit (`max_process_count=2`) | `process_limit`, peak=2, 0 leaks |
| Job memory (`24 MiB`) | `memory_limit`, 0 leaks |
| CPU user-time (long burn, 0.2s) | `cpu_limit`, terminal enforced |
| CPU rate 5% | applied (`ENFORCED_THROTTLE`) |
| Workspace (`400 KiB`) | `workspace_limit`; peak observed **1 MiB** (poll race) |
| Output | `output_limit` @ 8192 |
| Ownership + limits | Job close still kills members |
| Capacity LIGHT×3 | gov off ~408 ms vs on ~431 ms mean |
| Pressure 1/2/4 | 0 leaks; at 4, PROCESS/DISK hit budgets as designed |

Interactive regression (15 RPS target, 0/1/2 exec) after governance:

| Cell | client p95 ms | achieved RPS |
|---|---|---|
| A_none | 359 | 15.0 |
| B_one | 164 | 14.5 |
| C_two | 262 | 15.0 |

No governance-specific interactive collapse vs A_none on this run
(`docs/execution-resource-governance-interactive.json`).

---

## Ownership × resource races

KillOnJobClose still terminates members when limits are also configured.
Contain-before-requeue unchanged. Resource-limit termination does not create a
second attempt. Cleanup remains idempotent.

---

## Decision

### KEEP + SELECTIVE RESOURCE LIMITS

Enforce what Windows Job Objects + the executor can actually enforce on this
host. Do not claim network/FS/cgroup isolation. Do not treat RSS observation as
memory enforcement unless `max_rss_mb` terminates.

Participant uploads remain **forbidden**.

---

## Security checkpoint

Still **not ready** for public hostile participant code:

- no network denial  
- no filesystem jail  
- no host-escape resistance claim  
- no multi-tenant adversarial guarantee  
- scrubbed env helps but is not a sandbox  

---

## What we prove / do not prove

**Prove:** which budgets are enforced vs observed; exceedance → terminal
classification; interaction with Job Object ownership; no process leaks in
tested corpus.

**Do not prove:** production capacity numbers; sandbox security; that workspace
polls cannot overshoot; that CPU rate alone protects the interactive plane.

## Reconsideration

- Deploy target without usable Job Objects → revisit  
- Need hard FS/net isolation → containers / stronger boundary  
- Workspace race unacceptable → OS volume quotas / separate volumes  
- Hostile public code → stronger execution boundary required  

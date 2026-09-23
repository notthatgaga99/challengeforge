# ADR 0008: Execution isolation, integration, ownership, governance, and boundary

## Status

Accepted:

1. **KEEP SUBPROCESS** — synthetic executor prototype  
2. **KEEP EXECUTION INTEGRATION** — opt-in durable `synthetic_execution`  
3. **KEEP + OS OWNERSHIP** — Windows Job Object `KILL_ON_JOB_CLOSE` + recovery
   containment before requeue  
4. **KEEP + SELECTIVE RESOURCE LIMITS** — Job Object + executor budgets; network/FS
   jail **not** claimed  
5. **BOUNDARY DECISION A** — Keep subprocess + Job Object for
   **trusted/internal execution only**. Do **not** enable hostile participant
   code until a stronger boundary exists. **Minimum for hostile code: B
   (container-based execution)** on Linux; escalate to microVM / dedicated
   hosts when threat or tenancy demands it.

Participant code remains **forbidden**. Default mode remains `legacy`.

## Context

Ownership and resource governance bound lifecycle and consumption for a trusted
corpus. They do **not** provide filesystem, network, or host-integrity isolation.
Boundary probes show the child can still read out-of-workspace files, use
loopback/DNS, and import platform code via trusted PYTHONPATH.

## Decision

1. Retain the current execution plane for trusted synthetic work only.  
2. Treat env construction as an **allowlist** (`execution_environment`).  
3. Document FS/net/host gaps explicitly; do not call Job Objects a sandbox.  
4. Require container-class isolation (and secret-free execution hosts) before
   any participant-code path.  
5. Do not install Docker/Kubernetes/microVMs in this milestone.

## Alternatives

| Option | Role |
|---|---|
| A Subprocess + Job Object | **Current** trusted envelope |
| B Containers (Linux) | **Minimum** hostile-code boundary |
| C MicroVM / VM | Stronger kernel separation |
| D Dedicated execution service | Blast-radius / multi-tenant ops |
| E Hybrid | Later scale |

## Evidence

- `docs/execution-isolation-boundary.md`  
- `docs/execution-resource-governance.md`  
- `docs/execution-ownership-recovery.md`  
- `scripts/execution_isolation_boundary_experiment.py`  
- `tests/test_execution_isolation_boundary.py`

## Trade-offs

**+** Honest threat model; no false sandbox claims; clear upgrade path  
**−** Participant execution deferred; Windows dev host ≠ Linux prod container semantics

## Reconsideration

Participant code required · multi-tenant shared hosts · need net/FS deny ·
Job Objects unavailable · kernel-escape threat dominates → C/D.

## Related

ADR 0003, ADR 0005; ownership, governance, evaluation pipeline docs.

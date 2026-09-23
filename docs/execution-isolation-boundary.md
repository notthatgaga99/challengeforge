# Execution Isolation Boundary — Hostile Participant Code

**Status:** Design + measured boundary probes  
**Decision:** **A — Keep subprocess + Job Object for trusted/internal execution only**  
**Hostile-code prerequisite:** **B — Container-based execution (minimum)** on a Linux
production target; escalate to microVM / dedicated hosts when threat or tenancy
demands it.  
**ADR:** `docs/adr/0008-execution-isolation.md`  
**Harness:** `scripts/execution_isolation_boundary_experiment.py`  
**Results:** `docs/execution-isolation-boundary-results.json`

> **Resource governance is not sandbox isolation.**  
> Ownership answers who kills the tree. Governance answers how much CPU/RAM.  
> This document answers what host powers a participant process must *not* have.

Participant uploads remain **forbidden**.

---

## 1. Adversary model

Treat participant code as able to be:

buggy · looping · CPU/memory exhausting · process-spawning · filesystem-probing ·
network-probing · env/credential-seeking · reading other submissions/workspaces ·
talking to other executions · privilege-escalating · attempting host escape.

Do **not** infer safety from the trusted synthetic corpus.

### Planes

```text
CONTROL PLANE                         EXECUTION PLANE
─────────────                         ───────────────
API / workers / Postgres              participant program
scheduler / artifacts / secrets       interpreter / compiler
host FS / other tenants               children / workspace / tmp
```

The security problem is: **nothing in the execution plane may become a confused
deputy for the control plane.**

---

## 2. Protected assets (C/I/A)

| Asset | C | I | A | Notes |
|---|---|---|---|---|
| Platform / DB / cloud credentials | critical | critical | — | Must never reach child |
| API secrets / tokens | critical | critical | — | Allowlist env |
| Other submissions / workspaces | critical | critical | high | FS isolation |
| Organizer data / artifacts | critical | critical | high | |
| Postgres / control DB | critical | critical | critical | Blast radius |
| Host filesystem / config | high | critical | high | |
| Host process table | high | high | high | Inspect/kill others |
| Scheduler / worker / API | — | critical | critical | Availability |
| Network identity | high | high | med | Egress = attack surface |
| Kernel / host integrity | — | critical | critical | Escape = game over |

Not every asset needs the same mechanism; all need an explicit story.

---

## 3. Required security properties

| Property | Meaning |
|---|---|
| Process isolation | Cannot inspect/control unrelated control-plane processes |
| Filesystem isolation | Cannot read/write platform, secrets, other workspaces, host config |
| Network isolation | No sockets/DNS/egress by default; scoped if ever required |
| Resource isolation | Cannot starve API/worker/Postgres/other evals |
| Identity isolation | No privileged credentials or useful host identity |
| Lifecycle isolation | Kill terminates descendants reliably |
| Cross-execution isolation | A cannot interfere with B |
| Host integrity | Cannot control host or supervisor |

Distinguish **resource control** (quotas) from **security isolation** (authority).

---

## 4. Current architecture assessment

| Property | Current mechanism | Guarantee | Gap |
|---|---|---|---|
| Process tree ownership | Job Object KillOnJobClose | Strong in tested Windows envelope | Nested-job quirks; not a security jail |
| CPU / memory / process count | Selective Job limits + watchdogs | Resource governance | Not confidentiality/integrity |
| Wall / stdout / stderr | Executor caps | Enforced | — |
| Workspace bytes | App poll | Enforced w/ race | Not FS jail |
| Filesystem | Unique temp dir + cwd | **Weak** | Parent dirs, host paths, platform source via PYTHONPATH |
| Network | None | **Unrestricted** | Loopback + DNS work today |
| Credentials | Env **allowlist** | Partial (no inherited secrets in tests) | PYTHONPATH / USERPROFILE still host-adjacent |
| Cross-execution | Unique workspace paths | Partial | Shared host FS/net/kernel |
| Host integrity | Subprocess | **Insufficient** | Same kernel, same user |

Measured probes (`BOUNDARY_PROBE`):

- secrets (`DATABASE_URL`, `AWS_SECRET_ACCESS_KEY`) **not** present in child ✓  
- forbidden file **outside** workspace **readable** ✗  
- parent directory **listable** ✗  
- loopback / DNS / network stack **usable** ✗  
- `import challengeforge` via trusted PYTHONPATH **possible** ✗ (ok for trusted corpus only)

---

## 5. Design space

| Arch | Security buy | Cost | Fit now |
|---|---|---|---|
| **A Subprocess + Job Object** | Lifecycle + selective resources | Low | Trusted/internal only |
| **B OS/container (Linux ns/cgroup/net/seccomp)** | FS/net/proc + resources | Medium | **Min for hostile code** |
| **C MicroVM / VM** | Stronger kernel boundary | High | If kernel escape dominates |
| **D Dedicated execution host/service** | Blast-radius separation | High | Multi-tenant / density |
| **E Hybrid** (scheduler → sandbox hosts) | Scale + separation | Highest | Later |

Windows Job Objects ≠ Linux namespaces. Production hostile execution should be
designed for the **Linux container model**, not assumed identical on the Windows
dev laptop.

---

## 6. Threat → control mapping

| Threat | Required control |
|---|---|
| Read platform secrets | Credential isolation + FS isolation + no host env |
| Read another workspace | FS namespace / ACL / separate mounts |
| Exhaust CPU / memory / processes | cgroup / Job / VM quotas |
| Access internet / localhost | Network namespace + default deny |
| Attack host kernel | Container hardening; escalate to VM if needed |
| Survive worker death | Process/job ownership (already) |
| Escape execution | Strong isolation boundary + dropped caps / seccomp |
| Import platform code | No platform PYTHONPATH; minimal runtime image |

A sandbox is a **collection of independently required controls**, not one feature.

---

## 7. Credential boundary

```text
Control plane
    │  narrow execution request (no secrets)
    ▼
Execution boundary
    │  allowlisted env only
    ▼
Participant / synthetic process
```

**Contract:** allowlist (`execution_environment.build_execution_env`), not
“scrub known bad names.” Forbidden secret names are stripped even if injected
via `extra`.

Trusted corpus may set `PYTHONPATH` to platform `src` so workloads run. That is
a **trusted-only privilege** and must be removed for hostile code.

---

## 8. Network boundary

**Current:** NOT ENFORCED (probe confirms).

**Required for hostile code — default deny:**

- no DNS, no sockets, no localhost, no private nets, no internet

**If challenges ever need network:** explicit egress allowlist, DNS policy,
bandwidth limits, still no control-plane credentials.

This milestone does **not** implement a network sandbox.

---

## 9. Filesystem boundary

Desired conceptual layout:

```text
/
├── runtime   (RO interpreter/tools)
├── challenge (RO)
├── input     (RO)
├── output    (RW)
└── tmp       (RW, quota)
```

Everything else inaccessible. Achievable via containers/VMs/mounts — **not** via
Windows directory permissions alone.

---

## 10. Lifecycle / ownership with a stronger sandbox

```text
QUEUED → CLAIMED → STARTING → RUNNING
  → SUCCESS | resource/security violation | FAILURE
  → TERMINATING → CLEANUP → TERMINAL
```

Preserve: `execution_attempt_id`, contain-before-requeue, duplicate prevention,
workspace ownership, deterministic-limit non-retry.

Stronger sandbox adds a layer:

```text
Worker → (optional supervisor) → Container/VM → participant process
```

Ownership must still answer: who kills the sandbox when the worker dies?

---

## 11. Cost model (qualitative)

| | Startup | Density | Ops | Debug | Security buy |
|---|---|---|---|---|---|
| A Subprocess | lowest | highest | low | easy | lifecycle/resources only |
| B Container | low–med | high | med | med | FS/net/proc + resources |
| C MicroVM | higher | lower | high | harder | stronger host boundary |
| D Remote service | varies | pooled | high | harder | blast radius |

Buy security per unit complexity; do not maximize sophistication.

---

## 12. Decision

### A — Keep subprocess + Job Object for trusted/internal execution only

**Safe to run now:** trusted synthetic workloads / internal evaluators under
existing ownership + selective resource governance.

**Not safe to run now:** arbitrary / hostile participant code.

**Smallest stronger boundary justified for hostile participant code:**  
**B — container-based execution** (Linux namespaces, cgroups, network deny,
read-only root, dropped capabilities, seccomp) on an execution host that does
not hold control-plane secrets.

Escalate to **C (microVM)** if kernel-escape risk dominates; to **D (dedicated
service)** when multi-tenant blast radius or org policy requires physical /
network separation.

Do **not** enable participant uploads until that boundary exists and is evidenced.

---

## 13. Reconsideration triggers

- Participant code becomes a required product feature  
- Network access required for challenges  
- Multi-tenant shared execution hosts  
- Job Object guarantees unavailable in production  
- Execution escapes current trust model  
- Organizer challenges need native toolchains inside the sandbox  
- Startup latency or density becomes dominant cost  
- Cross-host recovery required  

---

## 14. What we prove / do not prove

**Prove:** current plane scrubbed env; open FS/net/host gaps via safe probes;
ownership/governance remain valuable but incomplete for hostile code.

**Do not prove:** container/VM security; production multi-tenant safety; that
allowlisting alone is secret isolation under a compromised child.

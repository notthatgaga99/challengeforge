# Execution Isolation — Untrusted Work Boundary

**Status:** Design + measured synthetic prototype  
**ADR:** `docs/adr/0008-execution-isolation.md`  
**Harness:** `scripts/execution_isolation_experiment.py`  
**Results:** `docs/execution-isolation-results.json`  
**Code:** `src/challengeforge/execution/`

---

## Observation → Signal → Policy → Experiment → Decision

| Layer | Content |
|---|---|
| Observation | Control plane already queues/admits synthetic evaluation; artifacts are unread blobs |
| Signal | Need an **execution plane** with isolation ≠ resource governance |
| Policy | Design space first; synthetic hostile workloads before participant code |
| Experiment | Subprocess + process-tree cleanup + output/wall limits on this laptop |
| Decision | See § Decision (evidence-driven) |

---

## 1. Problem

We learned how to control **how much** expensive work enters the system
(Resource-Aware Runtime, progressive evaluation, experimental interactive feedback).

The next question is classical systems work:

> Can we safely control **what** that work is allowed to do once it starts?

ChallengeForge will eventually evaluate participant-produced code. Today it does **not**.
Evaluation is synthetic hash/sleep work inside the worker thread
(`docs/evaluation-pipeline.md` §12: evaluator does not execute participant code).

Goal remains: **maximum useful capability per unit of compute**, while preserving
correctness, isolation, and interactive responsiveness.

This milestone earns the statement:

> Give me untrusted *synthetic* workloads, execute them within a measured
> resource/security envelope, collect evidence, and keep the rest of the platform alive.

It does **not** yet earn arbitrary public submission execution.

---

## 2. Current state (what exists / what is missing)

### Existing flow (today)

```text
participant → submission (+ opaque artifact blob)
           → submit → Evaluation QUEUED (Postgres)
           → worker claim → asyncio.to_thread(synthetic evaluator)
           → SUCCEEDED | FAILED
```

Artifacts live under `{artifact_root}/submissions/...` via `LocalFilesystemStorage`.
The worker receives `artifact_key` but **never reads bytes** for scoring.

### Intended eventual flow

```text
participant
  ↓
submission
  ↓
artifact / repository
  ↓
evaluation request
  ↓
execution environment   ← MISSING
  ↓
tests / compiler / program
  ↓
evidence
  ↓
evaluation result
```

### Missing for participant code

1. Runner that unpacks/builds/runs artifact bytes  
2. Trust boundary (today: same worker process via `to_thread`)  
3. Sandbox (no container/VM/jail/network/FS policy)  
4. Execution contract (language, entrypoint, limits, exit→score)  
5. Structured stdout/stderr/artifact collection from a run  
6. Per-job wall/CPU/memory/process/output limits and tree cleanup  
7. Implemented `ExpensiveWork` (spec exists; no `run()`)  
8. Secrets scrubbing for child environments  

**Reuse:** durable queue, SKIP LOCKED claim, resource-aware **admission**,
artifact storage protocol, progressive stage hooks as future insert points.

---

## 3. Threat model

### Assets to protect

| Asset | Why |
|---|---|
| API process | Interactive plane; compromise → platform RCE |
| Worker process | Queue authority; compromise → evaluation forgery / host access |
| PostgreSQL | Source of truth for submissions/evaluations/identity |
| Host filesystem | Other artifacts, secrets, OS files |
| Other submissions / participants | Confidentiality + integrity |
| Host CPU / memory / disk | Interactive SLO + multi-tenant fairness |
| Network | Lateral movement, exfil, DoS amplifiers |
| Secrets / env vars | DB URLs, API keys, cloud tokens |
| Evaluation artifacts / organizer data | Integrity of scoring evidence |
| Submission integrity | Fingerprints, idempotency, auditability |

### Adversary / workload classes (do not collapse)

| Class | Examples | Goal of containment |
|---|---|---|
| **Malicious** | path traversal, env steal, Postgres probe, fork bomb, symlink escape | Hard deny / isolate |
| **Buggy** | infinite loop, leaked children, OOM by accident | Timeout + cleanup |
| **Resource-heavy** | legitimate large compile, big tests | Quotas + admission |
| **Legitimate slow** | long integration tests within budget | Wall timeout only if over budget |

### Hostile patterns considered

Infinite loops, fork bombs, process trees, huge allocations, CPU spin,
unbounded stdout/stderr/files, disk fill, recursive FS walk, host file reads,
env reads, DB/metadata access, outbound network, symlink/path tricks,
fast spawn storms, orphaned children, malicious tests, pathological-but-legit work.

---

## 4. Isolation boundary

```text
┌─────────────────────────────────────────────────────────────┐
│ CONTROL PLANE                                               │
│  API · Postgres · scheduler state · worker orchestration    │
│  interactive HTTP · artifact *metadata* · evaluation rows   │
└───────────────────────────┬─────────────────────────────────┘
                            │ claim / start / collect / complete
                            │ (narrow IPC: argv, exit, capped I/O, files)
┌───────────────────────────▼─────────────────────────────────┐
│ EXECUTION PLANE                                             │
│  untrusted program · compiler/tests · temp FS · children    │
│  generated files · stdout/stderr (bounded)                  │
└─────────────────────────────────────────────────────────────┘
```

**Critical invariant:** Failure or abuse inside the execution plane must not
directly compromise the control plane.

### Boundary strength options (not yet a choice)

| Boundary | Isolates | Weak against |
|---|---|---|
| Thread | Almost nothing | Everything shared |
| Subprocess | Address space, many handles | FS, net, same UID, process tree |
| Process group / job | Tree kill / accounting | Same as subprocess + better cleanup |
| Container | namespaces, cgroups, often net/FS | Kernel bugs, misconfig, breakout |
| VM / microVM | Stronger kernel boundary | Startup/ops cost |
| Separate host | Hardware / blast radius | Network API complexity |

ChallengeForge must pick based on **measured** need, not fashion.

---

## 5. Design alternatives (decision space)

Scores are qualitative for *this* laptop monolith context. Not a popularity ranking.

### 5.1 In-process execution

**Attractive:** zero startup, easy debug, shared caches.  
**Dangerous:** one bug/malicious payload = worker/API process compromise.  
**Cannot provide:** FS/net/secret isolation, independent crash domain,
honest CPU accounting vs event loop.

| Dimension | Notes |
|---|---|
| Isolation strength | None for hostile code |
| Startup / memory | Minimal |
| Ops / local-dev | Trivial |
| ChallengeForge fit | **Unacceptable** for participant code; OK only for trusted synthetic helpers |

### 5.2 OS subprocess

**Isolates:** address space, basic crash containment.  
**Does not:** shared UID FS/net/env by default; descendants may orphan.  
**Fit:** first prototype for *synthetic* hostile workloads + measurement.

### 5.3 Process group / session / Job Object

**Adds:** coordinated signal/kill of descendants; clearer ownership.  
**Windows:** Job Objects / `CREATE_NEW_PROCESS_GROUP` + recursive kill.  
**Unix:** `setsid` / process group + `killpg`.  
**Fit:** required companion to subprocess for cleanup invariants.

### 5.4 Containers

**Adds:** mount/network/PID/UTS/IPC namespaces, cgroups, capability drop.  
**Still not:** perfect security (kernel shared), zero-config safety, free ops.  
**Cost:** image pull, daemon, startup latency, Windows/Linux asymmetry.  
**Fit:** when FS/net/cgroup guarantees exceed what Job Object + chroot-ish workspaces give.

### 5.5 MicroVM / VM

**Stronger** hardware/virtualization boundary; **higher** startup and ops cost.  
**Fit:** multi-tenant production with hostile public code — *later*, if measured.

### 5.6 External execution service

**Strong** failure isolation; **adds** auth, queueing, network trust, version skew.  
**Fit:** when local host concurrency envelope is exhausted or blast radius policy demands it.
Not justified for the first prototype.

---

## 6. Isolation ≠ resource governance

| Concern | Question | Example mechanisms |
|---|---|---|
| **Isolation** | Can it access something forbidden? | namespaces, drop caps, no shared mounts, scrubbed env, no net |
| **Resource governance** | Can it consume more than allowed? | CPU quota/shares, wall timeout, memory limit, nproc, fsize, output cap, disk quota |

Being “in a container” does **not** imply limits are enforced — misconfigured
cgroups still starve the host. Conversely, hard wall timeouts help buggy loops
but do not stop reading `/etc/passwd` if the FS is shared.

ChallengeForge must configure **both** deliberately.

### Limit → property map (prototype targets)

| Limit | Property |
|---|---|
| Wall-clock timeout | Bounded lifetime |
| Output byte cap | Unbounded stdout/stderr DoS |
| Process-tree kill | Orphan descendants |
| Workspace root + path checks | Casual path escape (soft) |
| Scrubbed child env | Casual secret leak via env |
| Admission concurrency (existing) | Host overload from *starts* |
| Memory soft-watch + kill | Soft RSS governance (not kernel hard limit on all OS) |

Hard cgroup memory/CPU require Linux containers or Job Objects — document OS gaps.

---

## 7. Process lifecycle

```text
QUEUED → STARTING → RUNNING → SUCCEEDED → COLLECTING → COMPLETED
                         ↓
                      TIMEOUT → KILLING → CLEANUP → FAILED
                         ↓
                      CRASHED / OOM / LIMIT → KILLING → CLEANUP → FAILED
```

| Owner | Responsibility |
|---|---|
| Control plane (evaluation row) | Durable intent, attempts, terminal status |
| Executor | OS process tree, workspace, I/O caps |
| Worker | Bridge: start executor, write result metadata, never cancel solely for pressure |

**Cleanup proof:** no live PIDs in the job’s tree; workspace removed or quarantined;
cleanup may be retried (idempotent).

Crash domains: worker crash → existing stale RUNNING recovery; executor crash →
durable FAILED/REQUEUE policy; host crash → recovery on restart.

---

## 8. Cancellation semantics (do not conflate)

| Kind | Meaning | ChallengeForge stance |
|---|---|---|
| **Scheduling cancellation** | Do not *start* | Admission hold (resource runtime) — **keep** |
| **Evaluation cancellation** | Organizer/user stop | Explicit product action (future); not pressure |
| **Timeout termination** | Over wall budget | Execution plane MUST kill tree |
| **Infrastructure failure** | Executor/host gone | Durable state + recovery |

Resource-pressure policy remains: **never** destructively cancel already-running
evaluation solely because pressure rises. Timeouts are a **different** contract.

---

## 9. Filesystem model

| Question | Prototype answer |
|---|---|
| Source location | Copied/synthesized into per-run workspace |
| Root FS | Workspace only (cwd); no intentional host mounts |
| Writable? | Yes, under workspace; cleaned after |
| Outputs | Capped capture + optional files under workspace |
| Symlinks / `..` / abs paths | Threat-aware; soft containment via cwd — **not** a kernel jail |
| Over-limit / disk full | Fail controlled; cleanup best-effort → quarantine flag |

Threats deferred to stronger isolation if earned: device nodes, hard-link tricks,
TOCTOU races, mount escapes.

---

## 10. Network model

| Mode | Security | Repro | Deps install | Convenience |
|---|---|---|---|---|
| Full host net | Worst | Poor | Easy | High |
| Restricted outbound | Better | Medium | Policy-dependent | Medium |
| Allowlist | Better | Medium | Explicit | Medium |
| **No network** | Best default for hostile | Best | Requires offline deps | Low for some challenges |

**Decision for prototype:** inherit host network is **unsafe as a silent default**.
Synthetic workloads do not need net; treat **no network** as the design target.
Enforcement strength: none in pure subprocess without OS firewall/namespace —
document as a **gap** until containers/netns.

---

## 11. Secrets boundary

Assume participant code is **hostile to secrets**.

| Channel | Policy |
|---|---|
| Child environment | Minimal scrubbed env (`PATH`, `LANG`, `TMPDIR`→workspace); no `DATABASE_URL`, cloud keys |
| Files | No secrets in workspace; control-plane credentials stay in parent |
| Host metadata | Out of reach only with stronger isolation |
| “We won’t put secrets in env” | Necessary but **insufficient** alone |

---

## 12. Prototype architecture

```text
scripts/execution_isolation_experiment.py
        ↓
challengeforge.execution.ProcessExecutor
        ↓
subprocess (new process group / Windows process group)
        ↓
synthetic workload module (LIGHT, CPU_HEAVY, ...)
        ↓
bounded stdout/stderr + wall timeout + tree cleanup + workspace rm
```

**Not wired** into API submission evaluation. No arbitrary uploads executed.

Workloads: `LIGHT`, `CPU_HEAVY`, `MEMORY_HEAVY`, `SLEEP`, `LARGE_OUTPUT`,
`CHILD_PROCESS`, `TIMEOUT`, `MANY_FILES`, `FAILURE`.

---

## 13. Safety invariants (must prove)

1. **Timeout containment** — over wall budget → terminates  
2. **Process-tree containment** — no uncontrolled descendants after cleanup  
3. **Output containment** — stdout/stderr bounded  
4. **Filesystem containment** — soft: stays in workspace for cooperative paths; hard jail not claimed  
5. **Resource containment** — wall + output + tree enforced; hard mem/CPU OS-dependent  
6. **Control-plane survival** — execution pressure must not casually destroy interactive plane (measured)  
7. **Durable evaluation state** — executor failure must not erase evaluation rows when integrated; prototype records durable JSON results  
8. **Idempotent cleanup** — cleanup safe to retry  

---

## 14. Failure matrix

| Failure | Expected |
|---|---|
| Program timeout | kill tree → cleanup → terminal failure |
| Soft OOM / mem watch | kill → cleanup |
| Non-zero exit | collect capped evidence → failure |
| Child survives parent | tree cleanup removes descendants |
| Output limit | stop reading / kill → contained |
| Many files | workspace cleanup removes them |
| Executor/worker crash | durable control-plane recovery (existing) |
| Partial cleanup fail | retry; surface quarantine/error |

“Process exited” ≠ “safe.”

---

## 15. Experiments & capacity envelope

Raw: `docs/execution-isolation-results.json` (laptop run, wall timeout 5s).

### Suite (safety)

| Workload | Status | Wall | Startup | Cleaned | Leaked PIDs |
|---|---|---:|---:|---|---|
| LIGHT | succeeded | ~0.8 s | ~96 ms | yes | 0 |
| CPU_HEAVY | succeeded | ~1.6 s | ~93 ms | yes | 0 |
| MEMORY_HEAVY | succeeded | ~1.1 s | ~108 ms | yes | 0 |
| SLEEP | succeeded | ~2.1 s | ~211 ms | yes | 0 |
| LARGE_OUTPUT | output_limit | ~0.9 s | ~210 ms | yes | 0 |
| CHILD_PROCESS | succeeded | ~3.2 s | ~98 ms | yes | 0 |
| TIMEOUT | timeout | ~5.2 s | ~146 ms | yes | 0 |
| MANY_FILES | succeeded | ~1.3 s | ~119 ms | yes | 0 |
| FAILURE | failed | ~0.9 s | ~94 ms | yes | 0 |

All tracked invariants: **true**.

### Capacity (concurrent synthetic runs)

| Conc | Workload | Throughput /s | Mean wall ms | Mean cleanup ms | Leaks |
|---:|---|---:|---:|---:|---:|
| 1 | CPU_HEAVY | 0.60 | 1360 | 246 | 0 |
| 1 | SLEEP | 0.40 | 2168 | 270 | 0 |
| 2 | CPU_HEAVY | 0.91 | 1870 | 252 | 0 |
| 2 | SLEEP | 0.94 | 1839 | 237 | 0 |
| 4 | CPU_HEAVY | 1.70 | 2017 | 260 | 0 |
| 4 | SLEEP | 1.51 | 2334 | 257 | 0 |

Throughput still rose 1→4 on this host for CPU_HEAVY; mean wall grew under
contention. Envelope for *useful* concurrency remains an admission concern when
wired to the interactive plane — this run did not claim interactive p95 under
load (executor not yet on the evaluation hot path).

**Capacity rule:** find the point after which more concurrency reduces useful
throughput or interactive health — not maximum concurrency.

---

## 16. Decision

### KEEP SUBPROCESS

Laptop run (`docs/execution-isolation-results.json`): timeout, process-tree,
output, filesystem cleanup, and failure-path invariants held for the synthetic
suite. Concurrent CPU_HEAVY/SLEEP cells at 1/2/4 workers showed **0 leaked PIDs**.

| Claim | Status |
|---|---|
| Subprocess + tree cleanup sufficient for **synthetic** prototype | **Yes** |
| Hard network deny / cgroup memory | **Not claimed** (OS gap) |
| Soft FS jail | Soft only (workspace cwd + cleanup) |
| Participant code execution | **Forbidden** |
| Containers / microVM / remote executor | Not justified yet |

Product defaults unchanged: evaluation remains synthetic; executor is not wired
to submissions.

If container guarantees become necessary (mount/network deny), reconsider toward
`KEEP PROCESS + CONTAINER` with measurement — not fashion.

---

## 17. Limitations

- Windows lacks Linux cgroups/netns; hard mem/CPU/net denial not fully proven here  
- Soft FS containment ≠ jail  
- Shared-laptop noise  
- No real participant corpus yet  

## 18. Reconsideration triggers

- Need network deny / mount isolation that subprocess cannot provide → containers  
- Multi-tenant public hostile code → microVM / remote executor  
- Safety invariants fail under capacity tests → stop; do not expose submissions  
- Interactive plane collapses under N executors → tighten admission before scaling  

## 19. Explicit non-goals (this milestone)

No Redis, Kafka, Kubernetes, isolation microservice-for-fashion, LLM/RAG,
arbitrary public submissions, or untested “secure because Docker” claims.

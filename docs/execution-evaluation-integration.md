# Execution–Evaluation Integration

**Status:** Opt-in `synthetic_execution` mode; participant code **forbidden**  
**ADR:** `docs/adr/0008-execution-isolation.md` (updated)  
**Harness:** `scripts/execution_evaluation_integration_experiment.py`  
**Results:** `docs/execution-evaluation-integration-results.json`  
**Code:** `challengeforge.evaluation.synthetic_execution`, `challengeforge.execution.contract`

---

## Observation → Signal → Policy → Experiment → Decision

| Layer | Content |
|---|---|
| Observation | ProcessExecutor works in isolation; evaluation was still in-process synthetic |
| Signal | Need durable orchestration of real subprocess work without trusting uploads |
| Policy | Opt-in mode; trusted corpus only; keep QUEUED/RUNNING/SUCCEEDED/FAILED |
| Experiment | Corpus + tree + capacity + interactive pressure |
| Decision | See § Decision |

---

## 1. Execution contract

```text
ExecutionRequest
  execution_id, workload (allowlisted name), limits

ExecutionResult (process evidence)
  status, exit_code, wall/startup/cleanup, stdout/stderr bytes,
  truncation flags, peak_rss, process_count, leaked_pids, workspace_cleaned

EvaluationExecutionRecord (orchestrator disposition)
  outcome, execution, evaluation_terminal, retryable, score, failure_reason,
  result_metadata
```

**Separation**

| Concept | Owner | Question |
|---|---|---|
| Execution result | `ProcessExecutor` | What happened while the program ran? |
| Evaluation result | Worker / repository | What durable decision do we record? |

---

## 2. Evaluation / execution boundary

```text
submission (metadata.evaluation_mode=synthetic_execution,
            metadata.execution_workload=LIGHT|…)
  ↓
evaluation QUEUED (Postgres)
  ↓
worker claim → RUNNING
  ↓
run_synthetic_execution → ProcessExecutor (trusted workload only)
  ↓
map ExecutionOutcome → mark_succeeded | mark_failed | release_for_retry
  ↓
terminal evaluation (+ result_metadata.execution)
```

The executor does **not** own evaluation identity, retries, or terminal status.

---

## 3. Outcome taxonomy

| Outcome | Eval terminal | Retry | Score | Meaning |
|---|---|---|---|---|
| SUCCESS | succeeded | no | 100 | Clean run |
| NONZERO_EXIT | failed | no | none | Deterministic program failure |
| TIMEOUT | failed | no | none | Wall budget exceeded |
| OUTPUT_LIMIT | failed | no | none | stdout/stderr cap |
| RESOURCE_LIMIT | failed | no | none | Soft RSS watch trip |
| CLEANUP_ERROR | failed | no | none | Exit ok but leaks/unclean workspace |
| START_FAILURE / EXECUTOR_ERROR | requeue if attempts remain, else failed | yes→no | none | Infrastructure |

Execution failures are **not** silently labeled “participant wrong” — failure_reason is `execution_*`.

---

## 4. Lifecycle

Durable states unchanged: `QUEUED → RUNNING → SUCCEEDED|FAILED` (or RUNNING→QUEUED on retry/stale).

Internal worker phases: start → execute → collect → dispose. No new DB status values.

---

## 5. Failure semantics

| Case | Behavior |
|---|---|
| Worker crash before start | Stale RUNNING recovery → requeue/fail |
| Worker crash during execute | Children may orphan until OS reaps; see § Process-tree. Stale recovery requeues evaluation — **does not** by itself kill orphans |
| Timeout | Tree kill → cleanup → FAILED + evidence |
| Non-zero | FAILED + evidence |
| Cleanup incomplete | CLEANUP_ERROR → FAILED |
| Invalid workload name | FAILED (`invalid_execution_workload`) |

---

## 6. Retry semantics

| Class | Policy |
|---|---|
| Deterministic program (exit/timeout/output) | **No** retry |
| Infrastructure start/executor error | `release_for_retry` while `attempt_count < max_attempts` |
| Worker crash (stale RUNNING) | Existing recover_stale_running |

---

## 7. Evidence model

| Field | Storage |
|---|---|
| exit, outcome, wall, caps, cleanup, rss, previews | `evaluations.result_metadata` JSONB |
| Full unbounded stdout | **Not** stored (capped in executor) |
| Workspace files | Deleted after run; not promoted to artifact store in this mode |
| Public API | Score/status/failure_message only — raw execution dict is organizer/diagnostic |

---

## 8. Resource budgets

| Budget | Label |
|---|---|
| wall timeout | ENFORCED |
| stdout/stderr caps | ENFORCED |
| process-tree cleanup | ENFORCED |
| workspace cleanup | ENFORCED |
| scrubbed env | ENFORCED |
| peak RSS / process count | OBSERVED |
| network deny / cgroups / FS jail | NOT ENFORCED |

`evaluation_stale_after_seconds` must exceed wall timeout (+ margin) when this mode is enabled.

---

## 9. Progressive interaction

`synthetic_execution` is a **single-stage** opt-in path. It does not auto-map onto cheap/medium/heavy progressive stages in this experiment. Progressive modes remain unchanged. Future mapping is deferred until a controlled corpus needs multi-stage evidence.

---

## 10–14. Experiments

Raw: `docs/execution-evaluation-integration-results.json`

### Corpus

All allowlisted cases matched expected terminal status; **0** leaked PIDs in
execution evidence. CHILD_PROCESS succeeds with tree cleanup; Windows workspace
rmtree may still report `workspace_cleaned=false` without PID leaks (recorded,
not fatal).

### Process-tree under orchestration

Host scan after CHILD_PROCESS: `leaked_after=[]`.

### Capacity (CPU_HEAVY via durable queue, 2 workers)

| Conc | Throughput /s | Mean exec wall ms | Statuses |
|---:|---:|---:|---|
| 1 | ~0.34 | ~890 | succeeded |
| 2 | ~0.67 | ~850 | succeeded |
| 4 | ~0.69 | ~810 | succeeded |

Throughput plateaus ~2→4 under 2 workers (admission/worker occupancy), not a
claim of infinite scale.

### Interactive (15 RPS, 4s, isolated API/worker)

| Cell | Exec pressure | Client p95 | Achieved RPS |
|---|---:|---:|---:|
| A_none | 0 | ~90 ms | ~15.1 |
| B_one | 1 | ~112 ms | ~15.1 |
| C_two | 2 | ~52 ms | ~15.0 |

No interactive collapse under this small envelope on this laptop run. Not a
production SLO.

---

## 15. Security checkpoint

**Ready for public participant code?** **NO.**

Remaining gaps: network denial, cgroup CPU/memory, strong filesystem isolation,
multi-tenant hostile execution, host-escape resistance, first-class orphan
supervisor on worker death.

---

## 16. Decision

### KEEP EXECUTION INTEGRATION

Trusted `synthetic_execution` composes with durable evaluation state, contains
process trees under orchestration for the measured corpus, collects bounded
evidence, and did not destroy interactive health at 0–2 concurrent executions
on this host. Participant uploads remain forbidden. Network/cgroup still
**NOT ENFORCED**.

---

## 17. Limitations

- Windows host; no cgroup/netns claims  
- Orphan children after hard worker kill not fully supervisor-owned  
- Interactive coupling measured only under laptop noise  
- No participant uploads  

## 18. Reconsideration

- Process leaks under orchestration → STOP / isolate further  
- Need net/mount deny → containers  
- Public multi-tenant hostile code → stronger isolation before wiring uploads  

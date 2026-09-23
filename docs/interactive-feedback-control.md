# Interactive Feedback Control — Closed-Loop Protection

**Status:** Implemented, **experimental / opt-in only** (product defaults remain disabled)  
**Harnesses:**
- `scripts/interactive_feedback_experiment.py` (A/B/C portfolio)
- `scripts/interactive_hotpath_experiment.py` (async publish + signal correlation)  
**Results:**
- `docs/interactive-feedback-results.json`
- `docs/interactive-hotpath-results.json`  
**ADR:** `docs/adr/0007-interactive-feedback-control.md`

---

## Observation → Signal → Policy → Experiment → Decision

| Layer | Content |
|---|---|
| Observation | Server wall + pool wait on designated interactive HTTP paths |
| Signal | Rolling p95 (window) published to Postgres for the worker |
| Policy | Hysteresis WARN/CRITICAL floors on resource pressure / HEAVY admission |
| Experiment | A/B/C portfolio + hot-path sync vs async + correlation |
| Decision | **EXPERIMENTAL ONLY** (see § Decision) |

---

## 1. Problem

Hard-mix pressure showed the resource-aware runtime bounds CPU/memory/queue, but
`resource_interactive_p95_*` thresholds were **0** (disabled). Interactive p95
sometimes collapsed under HEAVY eval while the controller never saw an interactive SLO.

Question (v1):

> Can measured interactive latency become a useful feedback signal that causes
> expensive evaluation to yield compute **before** interactive traffic degrades badly?

Follow-up (this experiment):

> Can we publish the same cross-process hint **without** making every interactive
> request wait for Postgres, and is there a cheap server-side signal that tracks
> client-side degradation?

Goal: **interactive first**, then spend remaining compute on evaluation.

---

## 2. Why CPU/memory-only pressure was insufficient

| Observation | Implication |
|---|---|
| API and workers are separate OS processes | Logical bulkhead exists, but they still share laptop CPU |
| Abusive cell: workers ~85% CPU, interactive ~20 RPS / ~135 ms | Host contention, not API event-loop blocking alone |
| Other heavy cells: multi-second interactive p95 | CPU/queue signals did not always trip before UX collapsed |
| Progressive correctness stayed 100% | Quality was fine; the missing piece was **interactive SLO feedback** |

---

## 3. Audit — request-path publication (pre-async)

Exact path before this experiment:

```text
HTTP request
  ↓
middleware timing (wall clock)
  ↓
RollingLatencyWindow.record (in-process)
  ↓
every N samples: await persist_interactive_hint  ← SYNCHRONOUS on request
  ↓
Postgres evaluation_scheduler_state (INSERT ON CONFLICT + SELECT FOR UPDATE)
  ↓
worker claim tick reads hint
  ↓
InteractiveFeedbackController + merge_pressure
  ↓
HEAVY admission / adaptive max workers
```

| Question | Finding |
|---|---|
| Synchronous on request path? | Yes — `persist_interactive_hint` + `commit` in middleware `finally` when sample cadence hit |
| How often Postgres written? | Every `resource_interactive_persist_every_samples` (default 5; experiments used 3) |
| Lock/transaction? | Singleton row `id=1`, `SELECT … FOR UPDATE`, then update p95/sample_count |
| Min publication delay? | 0 beyond the write itself when cadence fires; otherwise up to N−1 samples |
| How worker discovers signal? | Each claim/poll tick via `read_runtime_state` |
| Publish failure? | Logged; request still completed; last good hint retained |
| Requests if publish unavailable? | Yes — record/persist errors are caught; interactive path continues |

**Contamination risk (documented):** sync persist competed with interactive work for pool connections and added latency on the hot path.

---

## 4. Signal candidates

| Candidate | Source | Status |
|---|---|---|
| **A — server interactive wall p95** | Middleware wall → rolling window | Current controller input |
| **B — DB pool wait** | `RequestProfile.pool_wait_ms` → separate rolling window; published as `pool_wait_p95_ms` (migration `0009`) | Measured for correlation; **not** wired as controller input |
| **C — event-loop / queue pressure** | No cheap existing metric beyond uvicorn internals | Not invented for this experiment |
| **D — in-flight pressure** | Process-local `begin_request`/`end_request` counters; `/debug/interactive-metrics` | Recorded in series; not a controller input |
| **E — composite** | Only if A and B are complementary | Deferred — evidence did not justify |

Goal: find a cheap server-side signal that moves **before or alongside** client p95.

---

## 5. Asynchronous publication (architecture)

```text
HTTP request
  ↓
measure wall (+ optional pool_wait from profile)
  ↓
update local rolling estimators  ← hot path ends here (async mode)
  ↓
request completes

        background publisher (interval configurable, default 250 ms)
                   ↓
          persist hint only if dirty + material delta
                   ↓
             worker reads hint from Postgres
```

| Knob | Default | Role |
|---|---:|---|
| `resource_interactive_publish_mode` | `async` | `sync` kept for A/B hot-path experiments |
| `resource_interactive_publish_interval_ms` | 250 | Publisher cadence |
| `resource_interactive_publish_min_delta_ms` | 25 | Skip no-op writes |
| `resource_interactive_persist_every_samples` | 5 | Sync-mode cadence only |

**Invariant:** In `async` mode the interactive request never awaits Postgres hint publication.

**Cross-process semantics preserved:** API process → Postgres hint → worker process. No in-process shared state for the authoritative hint.

**Stale-signal semantics:** On publish failure, last successful hint remains in Postgres; workers keep the last value they read; insufficient samples do **not** force the feedback controller to HEALTHY.

---

## 6. Policy (thresholds & state machine)

Product defaults: `warn=0`, `critical=0` → **disabled**.

Experiment thresholds (mode B/C):

| Knob | Experiment value |
|---|---:|
| warn | 300 ms |
| critical | 900 ms |
| recovery (hysteresis) | 220 ms |
| window | 5 s |
| min samples | 8 |
| sustain | 1 s |
| cooldown | ≥ adjust cooldown |

### Levels

```text
HEALTHY  → no interactive floor
WARN     → pressure floor PRESSURED (reduce adaptive concurrency)
CRITICAL → pressure floor DEGRADED (max_heavy=0; mode C: hold all new claims)
```

Already-running work is **never cancelled**. Submissions stay durable.

### Modes

| Mode | Control |
|---|---|
| A | Observe + publish p95; thresholds 0 (no interactive floor) |
| B | WARN/CRITICAL as above; CRITICAL → no new HEAVY |
| C | Same + `critical_hold_all` (no new evaluation starts) |

---

## 7. Experiments

### 7.1 Closed-loop A/B/C portfolio (prior)

Isolated topology: load-gen parent → API child → worker child → embedded Postgres.  
Progressive mode for evals: `fixed_progressive`.  
22 portfolio scenarios (A/B/C × hard mixes + abusive + burst).

| Mode | Mean client p95 | Max client p95 | Saturated | Agree | Stranded | Evals done |
|---|---:|---:|---:|---|---:|---:|
| **A** (off) | **66 ms** | 89 ms | **0** | 100% | 0 | 89 |
| **B** (feedback) | 561 ms | 2505 ms | 1 | 100% | 0 | 98 |
| **C** (strict) | 1178 ms | 3628 ms | 2 | 100% | 0 | 75 |

`p95_improvement_A_minus_B` ≈ **−496 ms** (B worse than A on mean).

**Missed intervention:** Mode B adversarial-medium — client p95 2505 ms saturated while published server p95 ~130 ms **below warn 300** → controller stayed `normal`.

### 7.2 Hot-path + correlation (this experiment)

Harness: `scripts/interactive_hotpath_experiment.py`  
Profile: `full` (hotpath + missed + correlate + control), duration 5s, 2 workers.  
Raw: `docs/interactive-hotpath-results.json`

#### Hot-path A/B (feedback off)

| Publish | Mean client p95 | Mean publish writes | Saturated cells |
|---|---:|---:|---:|
| sync | 6401 ms | 25.25 | 4/4 |
| async | 5937 ms | 18.0 | 4/4 |
| sync − async | **+464 ms** | | |

Async reduced mean client p95 and write count on this run (`measurable_hotpath_gain=true`),
but **both** arms were client-saturated (multi-second p95). Treat the delta as
noisy host evidence that async does not *hurt*, not as a product SLO win.

Server wall p95 stayed ~150–190 ms; pool wait p95 stayed ~5–9 ms while client
p95 was thousands of ms.

#### Missed-intervention reproduction

| Cell | Client p95 | Server wall p95 | Missed pattern |
|---|---:|---:|---|
| adversarial medium + async + feedback | 7071 | ~180 | **yes** |
| adversarial medium + sync + feedback | 5826 | ~219 | no (published max still &lt; 300 on some samples; cell still saturated) |

Async publication did **not** fix the correlation failure: client ≫ warn while
server wall &lt; warn → controller does not intervene.

#### Correlation (qualitative)

All three correlate cells: `missed_by_server_wall=true` and
`pool_wait_also_below_warn=true`. Pool wait did **not** lead client degradation.
In-flight was recorded but did not earn a controller swap on this dataset.

#### Control A/B/C (async publish)

All measured control cells remained client-saturated; feedback B/C did not
restore healthy interactive p95 on this host run. Correctness stayed intact
(`decision_agreement` / stranded checks in verdict).

---

## 8. Decision

### EXPERIMENTAL ONLY

The mechanism is real (publish → read → hysteresis → HEAVY hold / hold-all),
unit tests cover NORMAL/WARN/CRITICAL/recovery/cooldown, async publication keeps
cross-process semantics, and durability/correctness held across measured cells.

**Gate outcome (hot-path experiment):** `EXPERIMENTAL ONLY`

1. Async is the **default publish mode** (invariant: request never awaits hint write).
   Sync remains available for experiments. Mean client p95 was modestly lower for
   async on saturated cells; write count fell (~25 → ~18 mean). Not claimed as a
   production optimization beyond removing hot-path contamination.
2. Server wall p95 still misses client saturation (reproduced).
3. Pool wait / in-flight were observed; **neither** tracked client p95 better
   enough to justify `KEEP ASYNC, CHANGE SIGNAL` or a composite controller.

Do **not** enable product feedback thresholds. Do **not** add Redis/Kafka/LLM
because the signal is imperfect.

Product defaults after this experiment:

| Knob | Value |
|---|---|
| `resource_interactive_p95_warn_ms` | `0` |
| `resource_interactive_p95_critical_ms` | `0` |
| `resource_interactive_publish_mode` | `async` |

---

## 9. What improved / did not / broke

**Improved**

- Interactive hint publish moved off the request completion path (async default)
- Pool-wait + in-flight instrumentation available for correlation without new infra
- Explicit stale-signal / publish-failure semantics
- Correctness/durability invariants preserved in unit + experiment cells

**Did not improve**

- Reliable trip when client saturates but server wall stays &lt; warn
- Evidence to promote pool-wait or composite as the product signal
- Mean interactive protection vs feedback-off on the prior A/B/C portfolio

**Limitations**

- Shared-laptop noise; single cells can invert sync vs async latency
- Server wall ≠ client latency under connection/load-gen pressure
- Async adds up to ~publish_interval publication delay by design
- No production SLO claims; PostgreSQL is sufficient for *this* cross-process experiment only

---

## 10. What the measurements prove / do not prove

**Prove:** opt-in closed loop can raise pressure floor from a published interactive
hint without losing evaluations; async publication can keep the authoritative hint
in Postgres without awaiting it on every request.

**Do not prove:** product-ready interactive SLO protection; that async always
reduces client p95; that Redis is unnecessary in every deployment; capacity numbers;
that any particular threshold generalizes.

---

## 11. Failure behavior

| Case | Behavior |
|---|---|
| Async publish fails | Interactive requests continue; last good hint retained; failure counted |
| Sync publish fails | Same (caught in middleware) |
| Insufficient samples | Feedback controller stays HEALTHY (does not invent pressure) |
| CRITICAL | New HEAVY held (mode B) or all new claims held (mode C); running work untouched |
| Recovery | Hysteresis + cooldown; capacity returns after pressure falls |

---

## 12. Reconsideration triggers

- A cheap server-side signal **reliably** tracks client p95 under hard eval on this topology
- Async publish shows a **stable**, repeated client-p95 reduction vs sync with feedback off
- Mode B (or a candidate signal) beats mode A on hard cells **with** low saturation and 100% correctness
- Still no Redis/LLM unless architecture (not aesthetics) requires it

## Next evidence-driven problem

Do **not** add LLM/RAG yet. Either:

1. Find a better server-side interactive degradation signal that correlates with client p95, or  
2. Accept EXPERIMENTAL ONLY / DROP for closed-loop interactive feedback and keep Resource-Aware Runtime v1 + opt-in `fixed_progressive` as the earned stack.

---

## Appendix — portfolio scorecard snapshot

- Prior A/B/C: `docs/interactive-feedback-results.json` (22 scenarios, 2026-09-22)
- Hot-path / correlation: `docs/interactive-hotpath-results.json`

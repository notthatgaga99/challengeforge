# Interactive Feedback Control — Closed-Loop Protection

**Status:** Implemented, **experimental / opt-in only** (product defaults remain disabled)  
**Harness:** `scripts/interactive_feedback_experiment.py`  
**Results:** `docs/interactive-feedback-results.json`  
**ADR:** `docs/adr/0007-interactive-feedback-control.md`

---

## 1. Problem

Hard-mix pressure showed the resource-aware runtime bounds CPU/memory/queue, but
`resource_interactive_p95_*` thresholds were **0** (disabled). Interactive p95
sometimes collapsed under HEAVY eval while the controller never saw an interactive SLO.

Question:

> Can measured interactive latency become a useful feedback signal that causes
> expensive evaluation to yield compute **before** interactive traffic degrades badly?

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

## 3. Observation (signal definition)

**What is measured**

- **Server request wall time** (middleware start → response complete)
- Only **designated interactive paths** (`is_interactive_control_path`)
- Rolling window p95 (default 5s), minimum sample count (default 8)

**Included paths**

- `GET /api/v1/challenges/{id}`
- `GET /api/v1/users/{id}/submissions`
- `GET /api/v1/submissions/{id}/evaluation`
- `POST …/submissions` (create) and `POST …/submit` (acceptance)

**Excluded**

- Client RTT / load-generator scheduling delay
- Evaluation queue wait / worker execution time
- Health, static, non-interactive methods
- Benchmark setup/teardown outside the drive window

**Cross-process publish:** API writes `interactive_p95_ms` + `sample_count` to
`evaluation_scheduler_state` (migration `0008`). Workers read it each claim tick.
In-process only would not reach the worker process.

**Contamination risk:** persist runs on the interactive path every N samples.
That is documented as a cost of the experiment; product default keeps feedback off.

---

## 4. Policy (thresholds & state machine)

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

## 5. Experiments

Isolated topology: load-gen parent → API child → worker child → embedded Postgres.  
Progressive mode for evals: `fixed_progressive`.  
22 portfolio scenarios (A/B/C × hard mixes + abusive + burst).

### Verdict inputs (this laptop run)

| Mode | Mean client p95 | Max client p95 | Saturated | Agree | Stranded | Evals done |
|---|---:|---:|---:|---|---:|---:|
| **A** (off) | **66 ms** | 89 ms | **0** | 100% | 0 | 89 |
| **B** (feedback) | 561 ms | 2505 ms | 1 | 100% | 0 | 98 |
| **C** (strict) | 1178 ms | 3628 ms | 2 | 100% | 0 | 75 |

`p95_improvement_A_minus_B` ≈ **−496 ms** (B worse than A on mean).

Correctness: **100% agreement, 0 false early-pass, 0 stranded** across A/B/C.

### Notable cells

- Mode A abusive: healthy (~57 ms client p95); pressure saw `pressured` once (CPU/queue).
- Mode B abusive / adversarial_control: healthy (~32–58 ms); `normal`+`pressured`.
- Mode B adversarial medium: **saturated** (client p95 2505) while controller stayed `normal` — server observed p95 ~130 ms **below warn 300** (missed intervention).
- Mode C mostly_easy: reaction ~4.7 s to `pressured`, but client still saturated (p95 3357).

---

## 6. Decision

### EXPERIMENTAL ONLY

The mechanism is real (publish → read → hysteresis → HEAVY hold / hold-all),
unit tests cover NORMAL/WARN/CRITICAL/recovery/cooldown, and durability/correctness
held. **This HTTP matrix does not show interactive protection improvement** over
baseline A; mean/max client p95 were worse under B/C, and several degradations
were invisible to the server-wall signal at current thresholds.

Do **not** enable product defaults. Do **not** claim an SLO guarantee.

---

## 7. What improved / did not / broke

**Improved**

- Interactive p95 is now a first-class, inspectable published signal
- Explicit hysteresis/cooldown avoids single-sample flaps (unit-proven)
- CRITICAL can force `max_heavy=0` without cancelling in-flight work
- Correctness/durability unchanged

**Did not improve**

- Mean interactive client p95 vs mode A on this portfolio
- Reliable trip when client saturates but server wall stays &lt; warn
- Mode C (hold-all) did not win on interactive health here

**Limitations / breakage risks**

- Shared-laptop noise; A was already healthy on most cells this run
- Server wall ≠ client latency under connection/load-gen pressure
- Persist-on-request adds work on the interactive path
- Reaction latency only observed once (~4.7 s) in mode C

---

## 8. What the measurements prove / do not prove

**Prove:** opt-in closed loop can raise pressure floor from published interactive p95 without losing evaluations or regressing decision agreement.

**Do not prove:** product-ready interactive SLO protection; capacity numbers; that B/C are “better”; that Redis/cgroups are required (not evidenced as necessary yet—signal quality is the first issue).

---

## 9. Reconsideration triggers

- Server p95 tracks client p95 under hard eval, and B beats A on hard cells with low saturation
- Persist moved off the hot path (async/batched) and still helps
- Closed-loop interactive hint fed without self-inflicted latency

## Next evidence-driven problem

1. **Decouple publish from request completion** (async batch) and re-run A vs B on cells where A actually saturates; or  
2. Validate whether a different interactive signal (e.g. pool-wait / in-process queue depth) correlates better with client p95 — still no Redis/LLM unless that experiment fails for architectural reasons.

---

## Appendix — portfolio scorecard snapshot

Raw JSON: `docs/interactive-feedback-results.json` (22 scenarios, 2026-09-22 laptop run).

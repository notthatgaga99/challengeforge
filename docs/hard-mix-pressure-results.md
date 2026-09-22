# Hard-Mix Resource Pressure — Results

**Harness:** `scripts/hard_mix_pressure_experiment.py`  
**Design:** `docs/hard-mix-pressure-experiment.md`  
**Raw:** `docs/hard-mix-pressure-results.json`  
**Profile:** `portfolio` (38 scenarios), duration 5s interactive, 2 workers, isolated API / workers / load-gen.

## Decision gate

**KEEP FIXED progressive (opt-in) + resource-aware runtime. Do not promote `resource_aware_adaptive` as superior under HTTP pressure.**

| Criterion | Evidence |
|---|---|
| Decision agreement (B/C/D) | **100%** wherever compared; **0** false early-pass/fail |
| Stranded jobs | **0** after drain across the matrix |
| Compute savings | Collapse correctly on hard/adversarial; easy mixes still save |
| Interactive plane | **Not always healthy** at 20 RPS under concurrent eval on this laptop |
| Adaptive vs fixed | Savings ≈ fixed; **no clear interactive win** for adaptive |
| Abusive HARD/ADVERSARIAL | Interactive stayed healthy (20 RPS, p95 ~135 ms) while savings = **0%** |

Default product mode remains **`legacy`**. Progressive modes remain **opt-in**.

Real LLM/RAG work is **not** justified yet as a product default. The progressive *contract* survived scrutiny; shared-host interactive contention under heavy eval did not earn new infrastructure or a model stack.

---

## Setup

| Item | Value |
|---|---|
| Isolation | API child process + worker child process + parent load-gen |
| DB | Embedded PostgreSQL |
| Budgets | min_workers=1, max_workers=2, max_heavy=1, bypass=2, runtime on |
| Scheduling | `bounded_light_bypass` |
| Ground truth | `WorkloadKind` suffix; decision = score ≥ 60 |
| Safe exit | `confident ∧ safe_to_terminate` |
| Coalescing | In-process unit bench only (not HTTP path) |

**Do not conflate:** offered HTTP RPS ≠ achieved HTTP RPS ≠ evaluation arrival ≠ evaluation completion.

---

## Verdict inputs (from scorecard)

```json
{
  "fixed_progressive_mean_agreement": 1.0,
  "fixed_progressive_mean_savings": 0.3482,
  "hard_mix_mean_savings": 0.119,
  "false_early_pass_total": 0,
  "interactive_healthy_at_20rps": false,
  "adaptive_mean_savings": 0.4224,
  "adaptive_vs_fixed_savings_delta": 0.0742
}
```

`interactive_healthy_at_20rps=false` means **at least one** policy_mix cell at 20 RPS tripped saturation criteria (achieved < 70% offered or p95 ≥ 2s). That is an honest host/contention finding, not a silent pass.

---

## Policy × mix @ 20 RPS, medium eval pressure

Quality for progressive modes (B/C/D): agreement always 1.0; FEP/FEF always 0 when compared.

| Mix | Mode | Achieved RPS | Interactive p95 | Saturated? | Savings | Early-exit |
|---|---|---:|---:|---|---:|---:|
| mostly_easy | legacy | 11.4 | 3298 | yes | n/a | n/a |
| mostly_easy | always_expensive | 12.1 | 3822 | yes | 0% | 0% |
| mostly_easy | fixed_progressive | 13.4 | 3064 | yes | **92%** | 100% |
| mostly_easy | adaptive | **20.0** | **143** | no | **88%** | 100% |
| balanced | fixed_progressive | 20.0 | 106 | no | **58%** | 70% |
| balanced | adaptive | 19.8 | 281 | no | **58%** | 70% |
| difficult | fixed_progressive | 19.9 | 142 | no | **46%** | 60% |
| difficult | adaptive | 19.9 | 212 | no | **46%** | 60% |
| adversarial | fixed_progressive | 11.4 | 3313 | yes | **0%** | 0% |
| adversarial | adaptive | 11.9 | 3529 | yes | **0%** | 0% |
| heavy_dominated | fixed_progressive | 13.4 | 2273 | yes | **23%** | 29% |
| hard_70 | fixed_progressive | 19.2 | 270 | no | **16%** | 20% |
| hard_90 | fixed_progressive | 19.9 | 121 | no | **8%** | 9% |
| hard_90 | adaptive | 19.7 | 127 | no | **8%** | 9% |

**Reading:** Progressive savings track uncertainty. Adversarial/hard mixes spend full compute and still agree. Interactive saturation appears when the shared laptop is busy with HEAVY stages + HTTP — not because progressive “cheated.”

Cold-start / host noise: early `mostly_easy` cells saturated while a later adaptive `mostly_easy` did not. Treat single-cell interactive latency as **noisy**; prefer patterns across mixes.

---

## Answers to the constrained questions

### A — Does progressive reduce expensive computation?

**Yes, when uncertainty is low.** Easy/balanced/difficult save ~46–92%. Adversarial saves **0%**. Hard-90 saves ~8%. Mean fixed-progressive savings across policy_mix ≈ **35%**; hard-mix subset ≈ **12%**.

### B — Does it preserve decision correctness?

**Yes.** Decision agreement **100%**, false early-pass **0**, false early-fail **0**, stranded after drain **0**.

### C — Does resource-aware scheduling protect interactive traffic?

**Sometimes, not reliably on this shared host.** Abusive adversarial + high eval pressure kept interactive at ~20 RPS / p95 ~135 ms. Other heavy cells saw p95 multi-second. The controller bounded workers (admission holds observed); it did **not** guarantee interactive SLO under all hard mixes.

### D — Does adaptive progressive improve vs fixed?

**Not enough to prefer it.** Savings ≈ fixed. Interactive outcomes are mixed both ways. Keep adaptive code as a DEGRADED deferral experiment; **prefer `fixed_progressive` when opting into progressive.**

### E — When does each policy stop being useful?

| Policy | Stops being useful when… |
|---|---|
| legacy | You need v3 ground-truth agreement (it is not bound to `WorkloadKind`) |
| always_expensive | You care about compute cost on easy work |
| fixed_progressive | Workload is mostly HARD/ADVERSARIAL (savings → 0; still correct) |
| resource_aware_adaptive | You expected better interactive p95 than fixed — **not shown here** |

---

## Rate sweep (difficult, fixed_progressive, medium pressure)

| Offered | Achieved | p95 | Saturated | Reason to stop |
|---|---:|---:|---|---|
| 10 | 10.1 | 121 | no | — |
| 20 | 13.7 | 2469 | **yes** | achieved < 70% of offered |
| 40 / 60 | **not run** | — | — | sweep stopped |

Higher rates are **invalid on this run** — environment invalidated the measurement. Do not invent API capacity.

---

## Pressure / abusive / recovery

| Scenario | Mode | Pressure | Achieved | p95 | Sat | Agree | Savings | Stranded |
|---|---|---|---:|---:|---|---:|---:|---:|
| hard_70 | fixed | low | 20.0 | 187 | no | 1.0 | 31% | 0 |
| hard_70 | adaptive | low | 15.5 | 881 | no | 1.0 | 31% | 0 |
| hard_70 | fixed | high | 19.9 | 127 | no | 1.0 | 32% | 0 |
| hard_70 | adaptive | high | 20.0 | 291 | no | 1.0 | 32% | 0 |
| hard_70 | fixed | burst | 13.2 | 2227 | yes | 1.0 | 32% | 0 |
| hard_70 | adaptive | burst | 20.0 | 143 | no | 1.0 | 32% | 0 |
| **abusive** | fixed | high | **20.0** | **135** | no | 1.0 | **0%** | 0 |
| recovery | adaptive | burst | 13.9 | 2352 | yes | 1.0 | 51% | 0 |

Abusive drain: max queue depth **18**, ending queued **0**, pressure states seen: `normal`. Submissions remained durable; queue drained.

---

## Resources (abusive cell example)

| Process | CPU% | RSS MB |
|---|---:|---:|
| API | 10.0 | 92 |
| Workers | 84.7 | (worker process) |
| Load generator | 16.0 | 100 |
| DB connections | 10 | — |

API and load-generator CPU are separated. Worker CPU dominates under adversarial HEAVY stages — expected.

---

## Coalescing

100 waiters → 1 computation (in-process). **Not** on the default HTTP evaluation path; **not** cross-process. Documented limitation — no Redis added.

---

## What improved

- Quality×cost under **real HTTP + workers** confirms v3: savings without silent disagreement.
- Hard/adversarial mixes honestly spend compute.
- Abusive invariant: expensive eval can queue without destroying interactive availability **in the measured abusive cell**.
- Durability: zero stranded after drain.

## What did not improve

- Adaptive progressive is not a clear interactive win vs fixed under HTTP.
- Interactive p95 under concurrent HEAVY eval is **host-limited** and noisy; 20 RPS is not a guaranteed SLO with eval pressure on this laptop.
- Rate sweep beyond 10–20 RPS with eval pressure was invalidated.

## What broke / limitations

- Shared-laptop CPU contention between API, workers, and load-gen.
- Interactive saturation criteria tripped in multiple cells; measurements remain valid as stress evidence, not as capacity marketing.
- In-process coalescing unused on HTTP path.
- Fairness across participants not re-proven in this matrix (prior FIFO + bypass docs still apply).
- `resource_interactive_p95_*` controller inputs remain **0** (disabled) — interactive latency is not yet a closed-loop signal.

---

## Capacity envelope (this run)

| Signal | Observation |
|---|---|
| Interactive alone (prior isolation) | ~40 RPS class on quiet host |
| Interactive + medium hard-mix eval | **~10 RPS clean**; 20 RPS intermittent |
| With adversarial high pressure | 20 RPS possible when workers dominate CPU but API stays light |
| Above 20 with eval pressure | **Invalid** this run (stopped early) |

---

## LLM/RAG justified?

**Not yet.** Progressive architecture + safe-exit contract are strong enough to *design* future `ExpensiveWorkSpec` stages. Implementing retrieval/LLM now would amplify the expensive plane before interactive isolation under HEAVY work is stronger (process isolation or calibrated interactive p95 feedback).

---

## Next evidence-driven problem

1. **Closed-loop interactive hint:** feed measured interactive p95 into the resource controller (`resource_interactive_p95_warn_ms` / critical) and re-run the abusive + hard_90 cells; or  
2. **Process-level bulkhead measurement:** quantify whether moving HEAVY stages further from the API event loop (already separate OS process) still contends on CPU enough to need cgroup/OS priority — still no Redis/K8s.

Prefer (1) if the goal is to make adaptive control earn its keep. Prefer neither LLM nor new queues until interactive under hard eval is a deliberate policy, not an accident of host load.

# Adaptive Evaluation v3 — Audit of the v2 claim

**Date:** 2026-09-22  
**Code under audit:** progressive evaluation at commit `9ca2eb2`  
**No behavior changes in this document.**

---

## 1. What produced the reported 46–92% savings?

Savings came from **metadata-directed early exit**:

| Mix | Mechanism | Savings |
|---|---|---:|
| all `pass_confident` | Cheap returns `PASS_CONFIDENT` → finish after 1 cost unit | ~92% vs 13 |
| 50/50 confident/uncertain | Half exit at 1; half escalate | ~46% |
| 80/20 | Mostly early exit | ~77% |

Formula: `1 - actual_cost_units / (13 × jobs)`.

The experiment harness set `adaptive_scenario` explicitly. Confidence was **not inferred from evidence quality** — it was a label that *caused* early exit.

---

## 2. Optimistic / unrealistic assumptions

1. **Cheap confidence is omniscient.** `pass_confident` / `fail_confident` always match the intended outcome by construction.
2. **No measurement of decision agreement** vs full expensive evaluation.
3. **Score bands diverge by stage.** Early-exit pass scores land in 90–100; later stages under the same label often land mid-band — so “always expensive” and “progressive” can disagree on the numeric score even when labels match.
4. **Mixes under-represent ambiguity/hard cases** where cheap information is insufficient.
5. **Adaptive vs fixed looked identical** on savings because pressure deferral rarely changed completed cost once drained.
6. **Interactive p95 gains** partly reflect shorter worker occupancy, not proven product quality.

---

## 3. Does confidence correlate with anything meaningful?

Only with **`metadata.adaptive_scenario`** (or a hash bucket when absent).

It does **not** correlate with:

- artifact content
- real solution correctness
- agreement with a full CHEAP→MEDIUM→HEAVY ground truth

---

## 4. Can we “save compute” by exiting too early?

**Yes.** Any policy that treats `PASS_CONFIDENT` / `FAIL_CONFIDENT` as terminal without an explicit *safe-to-terminate* bit can exit early on a wrong cheap signal. v2 experiments never injected a “confident but wrong” case.

---

## 5. What correctness/quality guarantee exists today?

| Guarantee | Status |
|---|---|
| Submission durability | Yes |
| No silent skip of evaluation when progressive finishes | Yes (SUCCEEDED with score) |
| Same decision as full expensive path | **Not measured / not guaranteed** |
| Stage resume after crash | Current stage preserved; stage may redo |
| Fairness under HEAVY deferral | Bounded bypass only; no hard deadline fairness |

---

## 6. What is NOT measured (v2)

- Decision agreement vs always-expensive / ground truth
- False early-pass / false early-fail
- Ambiguous / hard / adversarial mixes
- Pressure transitions mid-evaluation systematically
- Fairness (old expensive vs many cheap)
- Stage-crash recovery matrix

---

## 7. Evidence that would falsify the design

- High compute savings **with** material decision disagreement
- Under hard/adversarial mixes, progressive ≈ always-expensive cost (no savings) **and** no interactive benefit
- Deferred HEAVY permanently starved
- Crash between stages → wrong SUCCEEDED or lost stage progress
- Complexity cost exceeds measured value → simplify to legacy / fixed single-shot

---

## 8. v3 mandate

Challenge the architecture with:

1. Harder deterministic workload kinds  
2. Explicit ground truth + decision agreement  
3. Safe early-exit contract (`safe_to_terminate`)  
4. Pressure / fairness / failure matrices  

Then **keep, retune, or simplify** based on evidence — not sunk cost.

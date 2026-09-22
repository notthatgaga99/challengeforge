# Adaptive Evaluation v3 — Results

**Claim:** Progressive evaluation can save compute **without silent decision disagreement**
on adversarial synthetic workloads, *given* an explicit safe-early-exit contract.

**Not claimed:** Real grading intelligence / LLM quality.

Audit: `docs/adaptive-evaluation-v3-audit.md`  
Raw: `docs/adaptive-evaluation-v3-results.json`  
Harness: `scripts/adaptive_evaluation_v3_experiment.py`

## Verdict (decision milestone)

**KEEP — with retune of the early-exit contract (already applied).**

| Criterion | Evidence |
|---|---|
| Decision agreement | **100%** across easy/balanced/difficult/adversarial/heavy mixes (fixed_progressive @ NORMAL) |
| False early-pass/fail | **0** |
| Compute savings | Mean ~**43%** under fixed_progressive NORMAL (range ~0% adversarial → ~88% easy) |
| Adversarial mix | Savings **0%** (must run full chain) but **no** false early-pass thanks to `safe_to_terminate=false` |
| Complexity | Justified: without safe-exit bit, adversarial cheap confidence would corrupt results |

v2’s 46–92% savings were real *cost* reductions but **optimistic** because confidence was a friendly label. v3 shows savings **collapse when work is hard**, and that is the correct behavior.

Default production mode remains **`legacy`**. Progressive modes stay **opt-in**.

## Scorecard highlights (NORMAL)

| Mix | Mode | Agreement | Savings | Early-exit | Escalation |
|---|---|---:|---:|---:|---:|
| easy | always_expensive | 100% | 0% | 0% | 100% |
| easy | fixed_progressive | 100% | **87.7%** | 100% | 20% |
| balanced | fixed_progressive | 100% | **57.7%** | 70% | 60% |
| difficult | fixed_progressive | 100% | **46.2%** | 60% | 80% |
| adversarial | fixed_progressive | 100% | **0%** | 0% | 100% |
| heavy_dominated | fixed_progressive | 100% | **23.1%** | 30% | 90% |

## Pressure

DEGRADED/PRESSURED defer HEAVY then resume under NORMAL in the quality harness — agreement stays 100%; cost accounting includes resumed stages.

## Coalescing

100 waiters → 1 computation (in-process). **Not** global across API processes.

## What did NOT improve

- Adaptive mode does not beat fixed_progressive on savings when quality is held constant.
- Hard/adversarial mixes erase most savings — progressive is not a free lunch.
- Interactive/DB metrics in this harness are unit-level (no full HTTP matrix this round); prior isolation profile still stands.

## AI-ready contract (future)

```text
EvaluationStage
  workload_class, estimated_cost
  evidence, confidence, safe_to_terminate
  quality_target
```

Future: retrieval → rerank → small model → large model → code exec. **Not implemented.**

## Next evidence-driven problem

Closed-loop interactive p95 feedback was implemented and measured
(`docs/interactive-feedback-control.md`). Decision: **EXPERIMENTAL ONLY** —
correctness held, but mode A (feedback off) beat B/C on mean interactive p95
in the portfolio run. Prefer fixing signal quality / publish path before LLM/RAG.

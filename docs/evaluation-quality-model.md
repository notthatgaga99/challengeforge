# Evaluation quality model (v3)

## Ground truth

For synthetic `WorkloadKind`:

- `*_pass` → truth decision = PASS, truth score ∈ [70, 84]
- `*_fail` → truth decision = FAIL, truth score ∈ [10, 24]

Derived deterministically from `(kind, submission_id)` — no RNG.

Progressive **decision** = PASS iff `score >= 60`.

## Decision agreement

```text
agreement = progressive_decision == ground_truth.decision
```

## False early exits

Counted only when a **safe_early_exit_*** occurred:

- false early-pass: early exit PASS but truth FAIL  
- false early-fail: early exit FAIL but truth PASS  

## Safe early-exit contract

```text
terminal early exit
  ⇔  confidence ∈ {PASS_CONFIDENT, FAIL_CONFIDENT}
  ∧  safe_to_terminate == true
```

Confident but `safe_to_terminate=false` (adversarial cheap signal) **must escalate**.

## Workload kinds

| Kind | Cheap | Medium | Heavy |
|---|---|---|---|
| EASY_* | safe confident = truth | — | — |
| AMBIGUOUS_* | uncertain | safe = truth | — |
| HARD_* | uncertain | uncertain | safe = truth |
| ADVERSARIAL_* | confident **wrong**, unsafe | uncertain | safe = truth |

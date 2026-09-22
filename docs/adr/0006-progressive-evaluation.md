# ADR 0006: Progressive evaluation on a single durable evaluation row

## Status

Accepted (synthetic experiment / architecture).

## Context

We need staged CHEAP→MEDIUM→HEAVY evaluation with early exit and pressure-aware
deferral without introducing a workflow engine or Redis.

## Decision

**Option A:** one `evaluations` row with:

- `current_stage`
- `evaluation_mode`
- `deadline_at` (optional)
- `result_metadata.progressive` accounting

Default mode remains `legacy` (single-shot evaluator). Progressive modes are
opt-in via Settings / submission metadata for experiments.

## Alternatives

- Separate stage/job rows — more audit, more complexity  
- Workflow engine — unjustified  

## Evidence

Design: `docs/adaptive-evaluation-v2.md`  
Results: `docs/adaptive-evaluation-results.md`

## Trade-offs

+ One job identity, fits existing claim/SKIP LOCKED  
− Crash mid-stage may redo current stage (idempotent synthetic work)  

## Reconsideration

Promote progressive to default only if savings are material and fairness holds
under adversarial mixes; split stage rows if audit/compliance requires it.

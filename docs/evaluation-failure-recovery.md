# Evaluation failure / recovery (progressive stages)

## Durable representation

One `evaluations` row:

- `current_stage` — next stage index to run  
- `evaluation_mode`  
- `result_metadata.progressive` — accounting (not participant-facing)  
- status machine unchanged: QUEUED ↔ RUNNING → SUCCEEDED|FAILED  

## Crash points

| When | Behavior |
|---|---|
| After claim, before stage persist | Stale RUNNING → requeue; `current_stage` unchanged → redo stage |
| After CHEAP, before SUCCEEDED/defer commit | Same — may redo CHEAP (synthetic idempotent) |
| After defer_escalation commit | QUEUED with advanced `current_stage` + heavier `workload_class` |
| During HEAVY | Stale requeue at same stage |

## Guarantees

- Submission never lost  
- No second evaluation row (unique submission_id)  
- SUCCEEDED only via conditional RUNNING+worker_id update  
- Deferred work remains durable QUEUED  

## Limitation

Mid-stage crash does not checkpoint *within* a stage; the stage may re-execute.
That is acceptable for synthetic CPU work; revisit for non-idempotent LLM calls.

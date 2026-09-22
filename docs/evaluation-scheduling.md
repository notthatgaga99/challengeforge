# Evaluation scheduling

Generated from `docs/evaluation-scheduling-results.json` on 2026-09-22.
Historical concurrency, correctness, evaluation, capacity, and resource reports
were not modified.

## 1. Current behavior and observed problem

ChallengeForge remains PostgreSQL-backed and FIFO by default (`created_at`,
then `id`). The resource experiment showed a real head-of-line effect: a
HEAVY evaluation can occupy the safe HEAVY capacity while short LIGHT work is
waiting.

Workload classes (`LIGHT`, `MEDIUM`, `HEAVY`) are controlled evaluator labels,
not hardware guarantees or production cost predictions.

## 2. Scheduling policy

The policy is:

> FIFO by default. If the oldest queued job is a blocked HEAVY job, at most two
> consecutive LIGHT jobs directly behind it may bypass it.

`MEDIUM` never bypasses. The scheduler never scans deeper to find preferred
LIGHT work. Therefore LIGHT is not a general priority lane.

### Exact definition of blocked

A HEAVY job is blocked only when:

1. it is the oldest queued evaluation; and
2. `evaluation_max_concurrent_heavy` HEAVY evaluations are already RUNNING; and
3. total RUNNING work is below `evaluation_max_workers`.

A HEAVY label alone does not make a job blocked. If the HEAVY slot is free, the
oldest HEAVY job is claimed normally.

### Bound and starvation prevention

The configured bypass bound is **2**. A durable singleton scheduler-state row
stores the blocked HEAVY id and committed bypass count. Once two LIGHT jobs
have bypassed, workers deliberately claim nothing from behind that HEAVY until
a HEAVY slot becomes free. The HEAVY job is then claimed before later work.

This gives bounded LIGHT responsiveness and bounded HEAVY bypass. It is not a
claim of strict fairness.

If another HEAVY or a MEDIUM job is directly behind the blocked HEAVY, no scan
occurs and no LIGHT job farther back bypasses either one.

## 3. Worker capacity assumptions

Development defaults:

- `evaluation_max_workers = 2`
- `evaluation_max_concurrent_heavy = 1`
- `evaluation_light_bypass_limit = 2`
- `evaluation_scheduling_policy = "bounded_light_bypass"`

Two workers came from the measured useful peak on the test laptop. These
values are environment-specific, explicit configuration—not auto-scaling.
ChallengeForge does not dynamically tune from CPU or memory.

## 4. Database claiming and recovery

PostgreSQL remains the source of truth. Each claim transaction:

1. locks the singleton `evaluation_scheduler_state` row;
2. checks the global RUNNING and HEAVY RUNNING counts;
3. locks and selects the FIFO candidate (or its immediate LIGHT successor);
4. updates the evaluation to RUNNING with worker ownership; and
5. commits before evaluator CPU/memory work starts.

The short state-row lock serializes claim decisions across processes. It does
not remain held during evaluation. Completion still occurs in a separate
transaction and requires matching worker ownership.

Stale RUNNING recovery, attempt limits, evaluator failure semantics, and
durability are unchanged. A bypassed row remains an ordinary durable
evaluation. A claimed LIGHT crash consumes a bypass slot conservatively; the
row is recovered through the existing stale-RUNNING path.

## 5. Participant-facing semantics

Participants still see only Queued, Running, Complete, or Failed.
The participant evaluation/queue response does not expose workload class,
workers, CPU, or scheduling decisions.

The old precise-looking `position` field is replaced by:

`estimated_jobs_ahead`

This is a point-in-time count in default FIFO order, not a guaranteed execution
position. The UI says “Approximately N job(s) ahead.”

Per-submission `estimated_wait_seconds` is currently `null`. Dividing a FIFO
position by a global service rate is not defensible with heterogeneous,
bypassable work. A false-precision predictor was not introduced. The overall
backlog endpoint retains its clearly approximate capacity estimate.

## 6. Organizer visibility

The organizer queue adds:

- LIGHT queued
- MEDIUM queued
- HEAVY queued
- configured worker capacity

The participant backlog endpoint does not expose these class counts.

## 7. Paired experiment

Environment: Windows 10; Python 3.11; 10 physical / 12 logical CPU; about
16 GB RAM; embedded PostgreSQL; two workers; one concurrent HEAVY; two LIGHT
bypasses. Two paired repetitions used the same 16-job pattern:

`H H L L L M L H H L L M L H L M`

The small sample is noisy. Values below are means of the two runs.

| Metric | Pure FIFO | Bounded bypass |
|---|---:|---:|
| Throughput (eval/s) | 2.254 | 2.274 |
| Worker utilization | 53.02% | 63.39% |
| LIGHT wait p50 | 2641 ms | 2433 ms |
| LIGHT wait p95 | 5294 ms | 5864 ms |
| MEDIUM wait p50 / p95 | 4772 / 6150 ms | 4149 / 6192 ms |
| HEAVY wait p50 / p95 | 2811 / 5124 ms | 2791 / 5279 ms |
| LIGHT execution p50 | 124 ms | 205 ms |
| App CPU max | 140.9% | 129.5% |
| Process RSS max | 105.3 MB | 89.6 MB |
| DB connections max | 6 | 6 |
| Starvation incidents | 0 | 0 |
| Deadlocks | 0 | 0 |

The bypass policy improved mean LIGHT p50 by about 8% and changed early
completion order as intended, but LIGHT p95 worsened about 11%. Throughput was
effectively unchanged. Execution-time differences and RSS variation show
laptop/run noise, not a scheduling guarantee.

The result is therefore a trade-off, not evidence that bounded bypass is
universally faster. Its primary value is the explicit responsiveness bound
for the immediate LIGHT successors while retaining a starvation bound for
HEAVY.

## 8. Rejected alternatives

- arbitrary LIGHT priority
- dynamic CPU or memory prediction
- weighted fair queuing or round robin
- worker auto-scaling
- resource-aware admission control
- Redis, Kafka, RabbitMQ, Celery, Temporal, Kubernetes, or distributed locks

None is justified by this experiment.

## 9. Remaining limitations

- A first HEAVY job is not bypassed when the HEAVY slot is free.
- One-worker HEAVY→LIGHT head-of-line blocking remains; no spare capacity
  exists to make progress safely.
- Only consecutive LIGHT successors can bypass. This is deliberately narrow.
- The scheduler-state row serializes claims; appropriate at two workers, not
  presented as a high-scale design.
- A crashed bypassing LIGHT conservatively consumes one bypass until the
  blocked HEAVY is serviced.
- Queue-ahead and wait semantics remain estimates.
- The comparison has two paired repetitions and synthetic workloads.

## 10. Conclusion and next problem

The added complexity is justified narrowly: one table row, one bounded rule,
and explicit capacity replace an observed unbounded LIGHT delay with a rule
that is still explainable on a whiteboard. The data does **not** justify a
general scheduler.

The next natural problem is product validation: determine whether the measured
LIGHT latency distribution is meaningfully better for real participant
workloads. If not, remove or retune the policy rather than adding more
scheduling mechanisms.

# Evaluation capacity report

Generated: 2026-09-21T16:54:11.203131+00:00
Database: embedded PostgreSQL (16.2)
Evaluator fake work: 50 ms

Historical concurrency baseline and correctness reports were **not** modified.

## Workload model

- Continuous Poisson-like fixed-interval arrivals at 1 / 5 / 10 / 20 /s
- Sustained overload: 40/s with 1 worker (bounded)
- Worker scaling: 1 / 2 / 4 / 8 workers at ~15 arrivals/s
- Burst: 400 concurrent submit operations, 4 workers
- Fake evaluation duration: 50 ms (deterministic sleep)
- Safety: max evaluations / runtime / queue depth caps

## Arrival vs service rate (4 workers)

| Scenario | λ obs | μ obs | queue trend | peak depth | wait p50 | wait p95 |
|---|---:|---:|---|---:|---:|---:|
| arrival_1_per_sec | 1.042 | 0.986 | stable | 1 | 91.1 | 173.11 |
| arrival_5_per_sec | 4.899 | 4.334 | stable | 2 | 83.6 | 201.8 |
| arrival_10_per_sec | 5.457 | 4.49 | growing | 19 | 1329.07 | 2669.67 |
| arrival_20_per_sec | 6.228 | 4.55 | growing | 45 | 4106.39 | 6502.91 |
| sustained_overload | 6.157 | 3.657 | draining | 76 | 9487.6 | 11719.27 |

## Worker scaling (~15 arrivals/s)

| Workers | μ obs | wait p50 | wait p95 | peak depth | app RSS MB | DB conns |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 4.153 | 4354.31 | 4924.72 | 40 | 94.52 | 13 |
| 2 | 4.347 | 3691.74 | 4742.22 | 34 | 95.04 | 13 |
| 4 | 4.078 | 3613.53 | 4896.45 | 28 | 95.57 | 18 |
| 8 | 4.206 | 802.04 | 1693.17 | 14 | 95.65 | 24 |

## Burst (400 submissions)

- Submit phase: **93.48s**
- Total drain: **93.553s**
- Accepted: **400/400** (errors=0, concurrency=40)
- All submissions accepted independently of evaluation: **True**
- Submit latency p50/p95: 2891.4 / 13378.56 ms
- Initial queue after burst: `{'queued': 351, 'running': 1, 'succeeded': 48, 'failed': 0, 'oldest_queue_age_seconds': 80.432, 'db_connections': 16}`
- Evaluation health on submit responses: `{'normal': 23, 'busy': 92, 'saturated': 285}`

## PostgreSQL polling cost

```json
{
  "empty_queue": {
    "duration_seconds": 3.0,
    "poll_attempts": 30,
    "empty_polls": 0,
    "polls_per_second": 10.0,
    "claims": 30
  },
  "small_queue": {
    "queued": 5,
    "poll_attempts": 291,
    "empty_polls": 0,
    "claims": 291,
    "succeeded": 291
  },
  "large_queue": {
    "queued": 50,
    "poll_attempts": 85,
    "empty_polls": 3,
    "claims": 82,
    "succeeded": 82,
    "drain_seconds": 8.688
  }
}
```

## Fairness

```json
{
  "challenge_a_queued": 40,
  "challenge_b_queued": 5,
  "completion_order_prefix": [
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A",
    "A"
  ],
  "first_b_position": 40,
  "a_completed_before_first_b": 40,
  "observed_policy": "global_fifo_by_created_at",
  "starvation_of_b": true,
  "note": "Workers claim ORDER BY created_at ASC globally. Challenge B waits behind A's earlier queue entries."
}
```

## Failure / recovery

```json
{
  "crash_after_claim": {
    "running_after_crash": 1,
    "recovered": 1,
    "final_queued": 0,
    "final_succeeded": 940,
    "final_failed": 0,
    "healer_claims": 12,
    "duplicate_execution_prevented": true
  },
  "evaluator_failure": {
    "jobs_failed": 1,
    "passed": true
  }
}
```

## Saturation point (this laptop / this config)

On this laptop/configuration, under this workload:

- With **4 workers** and **~50 ms** fake work, observed service rate under
  continuous HTTP-driven arrivals clustered around **~4.3–4.6 evaluations/s**,
  far below the theoretical `workers / work_time ≈ 80/s`. Claim + DB round-trips
  dominate the sleep.
- HTTP + application path limited *achievable* arrival rate to roughly
  **~5–7 submissions/s** even when targeting 10–40/s (producer concurrency helps
  but does not remove create+submit latency).
- When observed λ exceeded μ (`arrival_10_per_sec`, `arrival_20_per_sec`),
  queue trend was **growing** and wait rose into seconds (p50 ≈ 1.3–4.1 s).
- `sustained_overload` peaked at depth **76** then **drained** after arrivals
  stopped — backlog is durable and recoverable, not lost.
- When arrival stayed near or below service capacity (`arrival_1`, `arrival_5`),
  the queue stayed **stable** with wait p50 under ~200 ms.

## Scaling behavior

Adding workers from 1→8 at similar arrival intensity:

- Throughput (μ) stayed near the arrival-limited band (~4/s) — workers were not
  the scarce resource once λ≈μ.
- Queue wait improved sharply at 8 workers (wait p50 4354 ms → 802 ms) as
  parallel claim drained backlog faster.
- DB connections rose with worker count (13 → 24); RSS stayed ~95 MB.
- Diminishing returns: beyond matching arrival rate, extra workers mainly cut
  wait, not raise completion rate.

## Backpressure decision

Experiment evidence: a large evaluation backlog did **not** prevent durable submission acceptance (burst: all submitted).

Therefore the product policy is:

1. **Never reject submissions** because evaluation capacity is exhausted.
2. Expose `GET /api/v1/evaluations/backlog` and submit-time messaging: `normal` / `busy` / `saturated` / `critical`.
3. Estimated wait uses recent service rate + queue depth (approximate).
4. Evaluation rows remain durable in PostgreSQL even under CRITICAL.

## PostgreSQL queue observations

- Empty-queue polling is expected at ~`1 / poll_interval` attempts per worker
  (configured 0.1 s → ~10 polls/s). The first capacity run’s empty-queue sample
  was contaminated by leftover work; treat the ratio of empty polls under a
  drained DB as the idle cost signal.
- Under load, claim work dominates; queue wait—not polling—is what participants
  feel when λ > μ.
- No Redis/Kafka introduced: measured limits are **capacity**, **HTTP arrival
  ceiling**, and **FIFO fairness**, not durability failure.

## Architectural decision

**Keep PostgreSQL as the evaluation queue for this scale.**

Evidence: laptop-scale rates drain with a few workers; polling cost is observable but not the binding constraint versus queue wait under saturation; correctness invariants remain intact.

See ADR 0004.

## Remaining weaknesses

- Global FIFO can delay later challenges behind a large backlog (confirmed:
  challenge B waited until all 40 earlier A jobs finished).
- Time-based stale recovery is approximate.
- Idle polling wastes queries when the queue is empty.
- Estimated wait is not a guarantee.
- Submission create+submit latency under burst pressure is high (submit p95
  multi-second) even though acceptance remains correct — API/pool tuning is a
  separate concern from the job queue store.


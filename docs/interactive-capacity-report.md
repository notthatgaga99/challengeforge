# Interactive capacity under evaluation pressure

Generated 2026-09-22 from:

- `interactive-capacity-results.json` (five scenarios, two workers)
- `interactive-capacity-isolation-results.json` (one-worker intervention check)

Historical experiment reports were not changed.

## Workload model

The experiment attempted an open-loop rate of **100 HTTP requests/sec** for
eight seconds (800 requests/scenario), with up to 100 concurrent clients:

| Category | Share | Request examples |
|---|---:|---|
| Challenge reads | 60% | `GET /challenges/{id}` |
| Evaluation status | 15% | `GET /submissions/{id}/evaluation` |
| Participant history | 10% | `GET /users/{id}/submissions` |
| Submission creation | 10% | `POST /challenges/{id}/submissions` |
| Organizer operations | 5% | queue health / challenge submissions |

This is a controlled live-hackathon model, not a universal production mix.
Percentages, rate, duration, clients, pressure scenario, worker count, and
evaluation workload distribution are command-line configurable.

Safety limits cap duration at 20 seconds, each scenario at 3,000 requests,
the full run at 12,000 requests, clients at 150, workers at two, and pressure
jobs at 48.

## Environment

- Windows 10 (`10.0.26200`)
- Python 3.11.0
- Intel model 154: 10 physical / 12 logical CPUs
- 16,016 MB RAM; 1,456 MB available at experiment start
- Embedded PostgreSQL 16.2
- One uvicorn application process
- API pool: 8 base + 4 overflow
- Evaluation workers: two for the primary comparison
- Evaluation constraints: one concurrent HEAVY, bounded LIGHT bypass of two

Evaluation work ran through the existing separate worker session factory and
never held a claim transaction during CPU work.

## Evaluation-pressure scenarios

| Scenario | Initial jobs | Ongoing producer | Workload |
|---|---:|---:|---|
| Baseline | 0 | none | idle workers |
| Normal | 4 | 0.5 eval/s requested | 80% LIGHT / 20% MEDIUM |
| Sustained backlog | 24 | 2 eval/s requested | 50% LIGHT / 25% MEDIUM / 25% HEAVY |
| Burst | 48 | none | 75% LIGHT / 15% MEDIUM / 10% HEAVY |
| Mixed | 32 | none | 50% LIGHT / 25% MEDIUM / 25% HEAVY |

The producer itself shared the overloaded HTTP plane and created only two
additional sustained-backlog evaluations. The preloaded queue nevertheless
remained backlogged for the full measurement.

## Baseline

With no evaluation backlog:

- attempted: 800 requests
- successful: 799 (99.875%)
- timeouts: 0
- achieved completion rate: **10.288 requests/sec**
- latency p50 / p95 / p99: **2,407 / 8,762 / 13,496 ms**
- process CPU max: 140.2%
- process RSS max: 96.46 MB
- DB connections / active: 12 / 2
- exact peak API pool checkout: 4
- lock waiters max: 1; deadlocks: 0

The attempted 100 RPS was not sustained. The baseline alone accumulated work
and took 77.8 seconds to complete 800 requests. Therefore this environment
does **not** demonstrate 100 RPS capacity.

## Performance under evaluation pressure

| Scenario | Achieved req/s | Success | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|---:|---:|
| Baseline | 10.288 | 99.875% | 2,407 | 8,762 | 13,496 |
| Normal | 10.041 | 99.875% | 2,467 | 8,925 | 13,987 |
| Sustained backlog | 7.952 | 99.875% | 3,073 | 10,474 | 15,519 |
| Burst | 10.066 | 99.750% | 2,571 | 8,778 | 12,719 |
| Mixed | 10.177 | 100.000% | 2,488 | 8,954 | 13,745 |

Normal, burst, and mixed pressure were close to the already-saturated
baseline. Sustained pressure was materially worse:

- achieved rate decreased about 22.7%
- p50 increased about 27.7%
- p95 increased about 19.5%
- p99 increased about 15.0%

The degradation appeared across endpoint categories rather than one route.
Under sustained pressure, challenge reads had 10,609 ms p95 and evaluation
status had 10,976 ms p95.

## Evaluation behavior

| Scenario | Max queue | Ending queue | Active workers | Eval/s | Oldest queued |
|---|---:|---:|---:|---:|---:|
| Normal | 5 | 0 | 2 | 0.088 | 16.3 s |
| Sustained backlog | 25 | 5 | 2 | 0.199 | 101.1 s |
| Burst | 48 | 26 | 2 | 0.252 | 82.8 s |
| Mixed | 32 | 10 | 2 | 0.254 | 79.5 s |

The bounded worker budget held: active evaluations never exceeded two.

## Resource and database behavior

Memory was stable: process RSS stayed around 96–99 MB. PostgreSQL RSS was
observed process RSS (not exclusive physical memory) and rose as PostgreSQL
processes accumulated across scenarios.

The primary sustained run did not show database-pool starvation:

- baseline exact API pool peak: 4; sustained peak: 5
- configured API capacity: 8 + 4 overflow
- baseline/sustained DB connections: 12 / 12
- sustained lock waiters: 0
- deadlocks/conflicts: 0 / 0
- sustained DB active connections max: 2

Direct pool-checkout wait time is not exposed by SQLAlchemy's pool events and
was not measured. There were no pool timeout errors, and utilization remained
below the configured API pool budget. Separate pool budgets were therefore
not added.

Peak process CPU was noisy (baseline 140.2%, sustained 133.7%) and does not by
itself establish causality. The application, HTTP client, and in-process
threaded evaluator shared the Python process in this harness. The one-worker
intervention is stronger evidence that evaluation concurrency contributed to
combined-system contention, but it does not distinguish Python scheduling,
CPU cache/contention, or OS scheduling.

## Isolation intervention

The smallest tested intervention was reducing the explicit default evaluation
budget from two workers to **one**. No new queue, rate limiter, admission
controller, or database pool was introduced.

Under the same attempted 100 RPS sustained-backlog model:

| Metric | 2 workers | 1 worker |
|---|---:|---:|
| Achieved requests/sec | 7.952 | 11.567 |
| p50 | 3,073 ms | 2,038 ms |
| p95 | 10,474 ms | 7,186 ms |
| p99 | 15,519 ms | 10,326 ms |
| Success | 99.875% | 99.750% |
| Evaluation throughput | 0.199 eval/s | 0.145 eval/s |
| Ending evaluation queue | 5 | 17 |

Interactive throughput improved about 45% and p95 improved about 31%, while
evaluation throughput fell about 27%. This explicitly favors total interactive
usefulness and headroom over maximum evaluation throughput.

The application default is now `EVALUATION_MAX_WORKERS=1`. Environments that
repeat the experiment and have sufficient headroom may configure two. Worker
count remains static and environment-specific; there is no automatic tuning.

The worker already runs as a sibling process in normal deployment and owns a
separate SQLAlchemy pool. The experiment used a separate worker pool as well,
so no additional process/pool isolation was justified.

## Endpoint detail for the one-worker sustained run

- challenge reads p50/p95: 2,032 / 7,424 ms
- evaluation status p50/p95: 1,909 / 6,555 ms
- participant history p50/p95: 1,923 / 4,675 ms
- submission creation p50/p95: 2,139 / 7,597 ms
- organizer operations p50/p95: 2,484 / 7,840 ms

These are still poor interactive latencies. Reducing evaluation concurrency
protects headroom but does not solve the baseline HTTP capacity limit.

## Capacity statement

> On the tested Windows laptop, with one uvicorn process, embedded PostgreSQL,
> the documented 60/15/10/10/5 request mix, 100 concurrent clients, and a
> sustained mixed evaluation backlog, a one-worker evaluation budget completed
> approximately 11.6 HTTP requests/sec at 7.2-second p95 latency with 99.75%
> success. The attempted 100 requests/sec was not sustained.

This statement is specific to this harness and environment. It is not a claim
that ChallengeForge supports 100 RPS.

## Limitations

- One primary run per scenario and one intervention run; laptop noise is high.
- The load generator and threaded evaluator shared the application process.
- Embedded PostgreSQL startup and Windows/OneDrive behavior differ from a
  production host.
- The open-loop target overloaded the system; completion rate includes drain
  time after request generation.
- API pool checkout wait was not directly timed.
- The producer was slowed by the same overloaded HTTP plane.
- No filesystem artifact upload load was included.

## Next natural engineering problem

Profile the baseline interactive request path before adding infrastructure.
The immediate question is why simple reads saturate near 10–12 requests/sec
with multi-second latency despite spare pool capacity. Candidate causes must be
measured: duplicate identity/UoW sessions, embedded PostgreSQL latency,
single-process uvicorn behavior, HTTP-client scheduling, and transaction
duration. Do not add caching or horizontal scaling until that profile exists.

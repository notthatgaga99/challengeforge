# Resource capacity report

Generated: 2026-09-22T01:53:01.263235+00:00

Historical concurrency / evaluation / capacity reports were **not** modified.
Scheduling remained **FIFO**. No fairness or resource-aware admission was added.

## Environment

- Platform: `Windows-10-10.0.26200-SP0`
- Python: `3.11.0`
- CPU: Intel64 Family 6 Model 154 Stepping 4, GenuineIntel (logical=12, physical=10)
- Memory total/available MB: 16016.4 / 882.6
- Database: embedded PostgreSQL (16.2)
- Isolation: read committed
- Workload profiles: `{"light": {"cpu_iterations": 25000, "memory_mb": 1, "min_ms": 20}, "medium": {"cpu_iterations": 120000, "memory_mb": 8, "min_ms": 80}, "heavy": {"cpu_iterations": 350000, "memory_mb": 32, "min_ms": 200}}`
- Safety: `{"max_workers": 8, "max_evaluations": 48, "max_runtime_seconds": 120, "max_worker_rss_mb": 512}`

## Baseline (1 worker / LIGHT)

- Throughput: **2.473 eval/s**
- Queue wait p50/p95: 2336.89 / 2736.51 ms
- Execution p50/p95: 162.59 / 276.81 ms
- Peak worker RSS: 89.5 MB
- Peak worker CPU%: 0.0
- Resources: `{"samples": 40, "max_db_connections": 5, "max_db_active": 2, "max_db_lock_waiters": 0, "max_transaction_ms": 61.51, "max_pool_checked_out": 1, "max_pool_size": 8, "max_pool_overflow": -7, "max_app_cpu_percent": 138.4, "max_app_rss_mb": 89.5, "max_db_cpu_percent": 60.1, "max_db_rss_mb": 227.31, "deadlocks_delta": 0, "conflicts_delta": 0, "exact_peak_pool_checked_out": 1, "pool_checkouts": 52}`

## Worker scaling (LIGHT)

| Workers | eval/s | wait p50 | wait p95 | exec p50 | process RSS MB | app CPU% | DB conns |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 2.058 | 80.36 | 173.36 | 147.14 | 90.25 | 199.5 | 5 |
| 2 | 2.776 | 77.51 | 183.32 | 160.1 | 90.54 | 160.3 | 5 |
| 4 | 1.578 | 90.67 | 371.0 | 258.36 | 90.71 | 140.2 | 8 |
| 8 | 1.526 | 100.34 | 202.74 | 320.77 | 91.77 | 173.6 | 12 |

## Mixed workloads (FIFO)

- Pattern: `['light', 'light', 'medium', 'light', 'heavy', 'light', 'medium', 'heavy']` × truncated to 32
- Throughput: 1.209 eval/s
- Overall wait p50/p95: 152.45 / 825.06 ms
- By class: `{
  "light": {
    "count": 16,
    "queue_wait_ms": {
      "min_ms": 18.47,
      "mean_ms": 316.26,
      "p50_ms": 193.89,
      "p95_ms": 948.24,
      "max_ms": 948.24
    },
    "execution_ms": {
      "min_ms": 150.5,
      "mean_ms": 440.75,
      "p50_ms": 256.87,
      "p95_ms": 1036.05,
      "max_ms": 1036.05
    }
  },
  "medium": {
    "count": 8,
    "queue_wait_ms": {
      "min_ms": 29.66,
      "mean_ms": 126.8,
      "p50_ms": 74.88,
      "p95_ms": 295.65,
      "max_ms": 295.65
    },
    "execution_ms": {
      "min_ms": 590.49,
      "mean_ms": 653.16,
      "p50_ms": 649.75,
      "p95_ms": 741.33,
      "max_ms": 741.33
    }
  },
  "heavy": {
    "count": 8,
    "queue_wait_ms": {
      "min_ms": 28.95,
      "mean_ms": 115.56,
      "p50_ms": 69.03,
      "p95_ms": 341.31,
      "max_ms": 341.31
    },
    "execution_ms": {
      "min_ms": 1559.45,
      "mean_ms": 1801.25,
      "p50_ms": 1818.93,
      "p95_ms": 2030.19,
      "max_ms": 2030.19
    }
  }
}`

## Head-of-line blocking

- Order: `['heavy', 'light', 'light', 'light', 'light', 'light', 'light']` with **1 worker**
- Observed: Under single-worker FIFO, LIGHT jobs waited behind the leading HEAVY job even though each LIGHT execution is short.
- head_of_line_blocking_observed: **True**
- LIGHT wait p50: 1992.21
- HEAVY wait p50: 896.24
- LIGHT exec p50: 99.56

## Memory profiles (2 workers)

| Class | peak RSS MB | exec p50 | throughput |
|---|---:|---:|---:|
| light | 91.89 | 244.06 | 2.087 |
| medium | 91.89 | 617.51 | 1.19 |
| heavy | 91.89 | 1684.98 | 0.601 |

## API responsiveness (participant traffic during LIGHT load)

Probes: challenge GET, submission list, evaluation status.

- During 1 worker: `{"challenge_get": {"min_ms": 64.87, "mean_ms": 92.56, "p50_ms": 79.92, "p95_ms": 188.14, "max_ms": 188.14}, "submission_list": {"min_ms": 78.05, "mean_ms": 97.35, "p50_ms": 90.22, "p95_ms": 118.52, "max_ms": 118.52}, "evaluation_status": {"min_ms": 71.23, "mean_ms": 84.84, "p50_ms": 82.21, "p95_ms": 99.76, "max_ms": 99.76}}`
- During 4 workers: `{"challenge_get": {"min_ms": 69.09, "mean_ms": 124.57, "p50_ms": 112.95, "p95_ms": 202.99, "max_ms": 202.99}, "submission_list": {"min_ms": 68.0, "mean_ms": 132.78, "p50_ms": 119.05, "p95_ms": 311.47, "max_ms": 311.47}, "evaluation_status": {"min_ms": 79.4, "mean_ms": 109.11, "p50_ms": 105.44, "p95_ms": 160.46, "max_ms": 160.46}}`
- During 8 workers: `{"challenge_get": {"min_ms": 80.54, "mean_ms": 114.95, "p50_ms": 115.12, "p95_ms": 153.67, "max_ms": 153.67}, "submission_list": {"min_ms": 89.03, "mean_ms": 126.17, "p50_ms": 126.0, "p95_ms": 150.68, "max_ms": 150.68}, "evaluation_status": {"min_ms": 67.24, "mean_ms": 116.59, "p50_ms": 114.46, "p95_ms": 191.47, "max_ms": 191.47}}`

## Resource impact (CPU / memory / DB)

Labels: **process RSS** = experiment process; **PostgreSQL RSS** = summed postgres process RSS (observed, not exact physical exclusive use); **app CPU%** = process CPU from the monitor (can exceed 100 on multi-core).

| Scenario | app CPU% max | process RSS MB | Postgres RSS MB | DB conns max | lock waits | deadlocks |
|---|---:|---:|---:|---:|---:|---:|
| baseline_1_light | 138.4 | 89.5 | 227.31 | 5 | 0 | 0 |
| scale_1_light | 199.5 | 90.25 | 264.71 | 5 | 0 | 0 |
| scale_2_light | 160.3 | 90.54 | 265.25 | 5 | 0 | 0 |
| scale_4_light | 140.2 | 90.71 | 265.7 | 8 | 0 | 0 |
| scale_8_light | 173.6 | 91.77 | 321.57 | 12 | 0 | 0 |
| mixed | 151.2 | 123.88 | 378.84 | 11 | 0 | 0 |
| memory_light | 166.2 | 91.89 | 379.7 | 11 | 0 | 0 |
| memory_medium | 133.0 | 91.89 | 380.22 | 11 | 0 | 0 |
| memory_heavy | 166.2 | 91.89 | 381.23 | 11 | 0 | 0 |

## Capacity envelope (this laptop / this config)

On this laptop/configuration, under these experimental workload profiles:

- Useful LIGHT throughput scaled with workers up to roughly **2** workers (2.776 eval/s in the best measured run).
- Recommended operating range for interactive ChallengeForge use: **2 evaluation worker(s)** for LIGHT-class experimental work, leaving headroom for API + PostgreSQL rather than chasing peak throughput.
- Headroom principle: do not configure workers at continuous CPU/memory saturation because participant API traffic and Postgres share the machine.
- Evidence for headroom choice: LIGHT throughput peaked at 2 workers; 4–8 workers increased exec time and DB connections while reducing throughput (CPU contention, not useful parallelism).
- RSS figures are **process RSS**, not exact physical RAM (shared pages mean summing processes overstates usage).

## Architecture conclusions

1. **FIFO still sufficient for the product today?** Yes as the default selection policy (clear, correct, durable). HOL under HEAVY-then-LIGHT is real with few workers, but does not by itself require replacing FIFO yet.
2. **Worker count main lever?** Yes for LIGHT throughput and wait — until returns diminish (here after ~2 workers).
3. **CPU-bound?** Yes for these synthetic workloads: beyond 2 workers, exec time rose and throughput fell while app/DB CPU stayed high.
4. **Memory-bound?** No at these bounded profiles (≤32MB alloc/job; process RSS stayed ~90–124 MB).
5. **Database-bound?** No as the primary bottleneck — 0 lock waiters, 0 deadlocks; connections rose with workers (5→12) but claim/complete transactions stayed short.
6. **HOL meaningful?** Yes under 1-worker FIFO with HEAVY ahead of LIGHT (LIGHT wait ~2s vs ~100ms exec).
7. **API interference?** Interactive GETs remained available; p50 latencies stayed roughly ~100–130 ms under load. Not "unusable," but workers share the machine — headroom still matters.
8. **Resource-aware scheduling justified yet?** Not yet — HOL is observable, but adding a scheduler is not earned until product priorities require preferring LIGHT over HEAVY.
9. **Fairness justified yet?** Not yet as a default. Cross-challenge / workload HOL is a known FIFO consequence; keep measuring before designing.
10. **PostgreSQL adequate as job store?** Yes for this scale — bottlenecks were evaluator CPU/concurrency and FIFO ordering effects, not queue storage durability.

## Next natural development problem

Decide whether product requirements treat HEAVY-ahead-of-LIGHT delay as acceptable UX. If yes, keep FIFO and tune worker count (~2 here). If not, the next earned complexity is a minimal fairness/class policy — still without Redis/Kafka/K8s.

## Limitations

- Workloads are synthetic (hash + bounded alloc), not real grading.
- Workers in the harness share one Python process via threads (`asyncio.to_thread`); production multi-process workers may differ.
- Single-run measurements (laptop noise); available RAM was low during the run.
- Worker-local `cpu_percent` samples can read 0.0; prefer monitor `max_app_cpu_percent` for process CPU.
- Did not measure GPU, disk IO saturation, or multi-tenant isolation.
- Did not modify historical experiment artifacts.


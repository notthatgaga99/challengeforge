# Interactive request-path profile

Investigation iteration only. No caching, Redis, indexes, pool enlargement,
replicas, or async API rewrites were applied.

Raw measurements:

- `docs/interactive-path-profile-results.json` (40 RPS attempt × dataset × workers)
- `docs/interactive-path-profile-baseline.json` (10 RPS clean baseline)

Instrumentation (opt-in):

- `Settings.request_profiling_enabled`
- `X-CF-Profile` JSON response header (pool wait, SQL, query count, stage walls)
- `scripts/interactive_profile.py`

---

## Environment

| Item | Value |
| --- | --- |
| Host | Windows 10/11 laptop (shared client + server process) |
| CPU | Intel Family 6 Model 154, 12 logical / 10 physical |
| RAM | ~16 GB total; during matrix run available fell to ~0.8–1.1 GB |
| Python | 3.11.0 |
| PostgreSQL | 16.2 (embedded `pgserver`) |
| API | FastAPI + Uvicorn in-process thread |
| DB pool | `pool_size=8`, `max_overflow=4` |
| Evaluation workers | scenario modes `0` and `1` |
| Profiling | enabled on API process only |

Limitation (explicit): the load generator (`httpx` async client) and the API
server share one Python process and one event loop. Client CPU often measured
near 70–100% during runs. Client-side saturation must not be read as server
capacity.

---

## Workload

Representative mix (equal rotation):

1. `GET /api/v1/challenges/{id}` — challenge read  
2. `GET /api/v1/submissions/{id}/evaluation` — evaluation status  
3. `GET /api/v1/users/{id}/submissions` — participant submission list  
4. `POST /api/v1/challenges/{id}/submissions` — submission creation  

### Dataset sizes

| Label | HTTP-seeded submissions | Extra queued rows (direct insert) |
| --- | ---: | ---: |
| small | 12 | 0 |
| realistic | 40 | 100 |
| large | 40 | 1000 |

Seeded evaluations are drained before the interactive drive so the interactive
plane is not intentionally evaluation-saturated (except where workers=1 and
new submissions enqueue work).

### Load-generator configurations

| Run | Target RPS | Duration | Clients | Purpose |
| --- | ---: | ---: | ---: | --- |
| baseline | 10 | 8 s | 8 | clean interactive baseline |
| matrix | 40 | 6 s | 40 | pressure + dataset-size × workers |

Safety caps in the script: duration ≤ 15 s, requests ≤ 800, clients ≤ 80.

---

## Baseline latency

**Primary baseline:** `small_workers_0` at 10 RPS / 8 clients.

| Metric | Value |
| --- | --- |
| Achieved RPS | **9.80** |
| Error rate | 0 / 80 |
| Timeout rate | 0 |
| Client p50 / p95 / p99 | 102 / 245 / 1388 ms |
| Server `total_ms` p50 / p95 / p99 | 38 / 68 / 1190 ms |
| Process RSS | ~89 MB |
| Client+server CPU (shared process) | ~74% |

Same workload with **workers=1**:

| Metric | Value |
| --- | --- |
| Achieved RPS | **9.92** |
| Client p50 / p95 / p99 | 112 / 170 / 191 ms |
| Server p50 / p95 / p99 | 44 / 79 / 98 ms |
| RSS | ~95 MB |
| CPU | ~100% |

Interpretation: at ~10 RPS the laptop/harness matches the prior interactive
capacity checkpoint (~10–12 RPS). Server path latency is tens of milliseconds;
client latency is higher but still sub-second at the median.

---

## Request-stage timings

Server header stages (baseline `small_workers_0`, p50):

| Endpoint | Server total | Pool wait | SQL | Non-DB | Queries |
| --- | ---: | ---: | ---: | ---: | ---: |
| challenge_read | 38 ms | 3.8 ms | 9.4 ms | 24.5 ms | 5 |
| evaluation_status | 29 ms | 3.9 ms | 7.1 ms | 17.8 ms | 3 |
| participant_history | 28 ms | 3.8 ms | 6.5 ms | 16.4 ms | 3 |
| submission_creation | 54 ms | 3.5 ms | 14.3 ms | 34.6 ms | 7 |

Accounting notes:

- `pool_wait_ms` and `sql_ms` are exclusive.
- `identity_ms` / `queue_position_ms` / `app_ms` / `serialize_ms` are inclusive
  stage walls and may contain SQL; they are reported for attribution, not summed
  into a closed exclusive budget.
- `non_db_ms = total_ms − pool_wait_ms − sql_ms`.

For completed evaluations, `queue_position` is not invoked (status ≠ queued), so
`queue_position_ms` is 0 on the drained evaluation-status path. Cost of that
query is measured separately below.

---

## DB pool wait

| Scenario | Pool wait p50 | Pool wait p95 | Notes |
| --- | ---: | ---: | --- |
| baseline workers=0 | 3.8 ms | 9.7 ms | not dominant |
| baseline workers=1 | 5.5 ms | 12.5 ms | still small vs total |
| matrix 40 RPS workers=0 | 4.5 ms | 12.6 ms | p99 spike to ~1 s (tail) |
| matrix 40 RPS workers=1 | 4.4 ms | 7.3 ms | cleaner than workers=0 tail |

**Verdict:** under the clean baseline, pool wait is low (~4–6 ms p50). Occasional
high p99 under overload is consistent with connection contention under event-loop
saturation, not steady pool exhaustion. No evidence of sustained pool starvation
as the primary interactive bottleneck at ~10 RPS.

---

## SQL timings

Baseline p50 SQL wall per request: **~6–14 ms** depending on endpoint.

Writes (`submission_creation`) spend more SQL time (~14–18 ms p50) than reads
(~7–13 ms), which is expected (auth + challenge load + insert + evaluation insert).

Dataset growth (small → large) did **not** materially inflate SQL p50 for the
same endpoints under the matrix:

| Scenario | SQL p50 | SQL p95 |
| --- | ---: | ---: |
| small_workers_0 | 11.0 ms | 36 ms |
| realistic_workers_0 | 11.3 ms | 41 ms |
| large_workers_0 | 11.1 ms | 42 ms |

---

## Query counts

Fixed per endpoint (not scaling with list length in these fixtures):

| Endpoint | Queries / request | Observed categories (typical) |
| --- | ---: | --- |
| challenge_read | **5** | identity, challenge, 2 relationship loads, hackathon (auth) |
| evaluation_status | **3** | identity + submission/evaluation lookups |
| participant_history | **3** | identity + submissions (+ related) |
| submission_creation | **7** | identity, challenge graph, inserts |

### N+1 assessment

- Challenge read uses `selectinload` for specs/criteria (**2 extra queries**,
  fixed — not O(N) rows of children beyond those relations).
- Challenge service also loads the parent hackathon for authorization (**1 query**).
- Participant history and evaluation status stay at **3** queries in these runs.
- No unbounded N+1 proportional to list size was measured in this dataset range.

**No N+1 fix applied** — fixed multi-query shapes exist, but they do not dominate
server latency relative to non-DB time and harness effects.

---

## Application-side timings

Baseline `non_db_ms` p50 ≈ **16–35 ms**, typically **larger than SQL** for every
endpoint. That bucket includes:

- FastAPI / dependency / middleware overhead
- ORM object construction and domain mapping
- Pydantic response building
- Identity stage wall outside pure SQL attribution
- Shared event-loop scheduling noise

Serialize-specific stage instrumentation is present but currently under-used
(most endpoints return ORM→schema conversion without an explicit
`timed_stage("serialize")` wrap). Treat `non_db_ms` as the application+framework
envelope, not a proven Pydantic-only cost.

---

## Queue-position query behavior

Repository `queue_position` (COUNT of queued jobs ahead) measured with workers
stopped after each interactive drive:

| Queue depth | Jobs ahead | Latency p50 | Latency p95 |
| ---: | ---: | ---: | ---: |
| 10 | 9 | ~11–12 ms | ~13–19 ms |
| 100 | 99 | ~11–13 ms | ~13–18 ms |
| 1000 | 999 | ~11–12 ms | ~13–51 ms |

`EXPLAIN` remains an Aggregate with planner cost rising modestly with depth
(~26 → ~50–90). Latency stays ~12 ms across 10→1000 under this embedded DB.

**Verdict:** queue-position cost is measurable but **not** the interactive-path
bottleneck at these depths. An index is **not** justified by this evidence alone.

---

## Dataset-size behavior

Interactive achieved RPS under the 40 RPS matrix stayed ~7 RPS across small /
realistic / large. Server SQL and total p50 did not scale up dramatically with
queued-row count for the profiled reads.

Conclusion for this range: latency is **not primarily driven by dataset size**.
Larger tables may still matter later; current evidence does not show it.

---

## Evaluation-worker comparison

| Mode | Baseline achieved RPS | Server p50 | Notes |
| --- | ---: | ---: | --- |
| workers=0 | 9.80 | 38 ms | clean interactive |
| workers=1 | 9.92 | 44 ms | similar throughput |

Under the overloaded 40 RPS matrix, workers=0 vs 1 also stayed near ~7 RPS with
similar stage mixes. Prior capacity work showed workers=2 hurting interactive
behavior more; this profile confirms workers=0/1 are close for the interactive
mix when evaluation backlog is not intentionally sustained.

---

## Load-generator limitations

Strong evidence that the laptop/harness limits observed “capacity”:

1. **Shared process / event loop** — client and server compete for the same CPU
   and asyncio loop.
2. **Client vs server gap**:
   - Clean 10 RPS: client p50 ~102 ms vs server ~38 ms (~2.5×).
   - Overloaded 40 RPS attempt: client p50 ~3–4 **seconds** vs server p50
     ~40–45 **ms** (orders of magnitude). Server work completes; client waits.
3. **Achieved RPS** collapses from target 40 to ~7 while server stage times stay
   tens–hundreds of ms — classic client/queueing saturation.
4. **CPU** of the shared process often ~70–100% during runs.
5. **Memory pressure** during the matrix (~0.8 GB available) may add OS noise.

Therefore: the prior “~10–12 RPS” figure is best read as **harness+laptop
interactive capacity**, not as proven PostgreSQL or FastAPI product capacity.

---

## Dominant latency contributors

Classification for the **clean baseline** (evidence-based):

| Category | Role |
| --- | --- |
| **E — load-generator / client / shared-host limitation** | Primary explanation for why attempted 40–100 RPS cannot be claimed; dominates overload client latency |
| **C — application / framework CPU (non_db)** | Largest exclusive server bucket at p50 (~half of server total) |
| **B — SQL execution** | Real but secondary (~1/4 of server total at p50) |
| **A — DB pool wait** | Low at p50; not the primary baseline bottleneck |
| **F — evaluation-worker contention** | Not dominant for workers 0 vs 1 on this interactive mix |
| **D — serialization** | Present inside non_db; not isolated enough to rank alone |

Overall label: **G — multiple contributors**, with **E** dominant for RPS ceilings
on this harness and **C+B** explaining server-side request time once the request
is being handled.

**No optimization was implemented** — no narrowly justified, correctness-neutral
hotspot cleared the “measure then fix once” bar.

---

## Confidence / uncertainty

| Claim | Confidence |
| --- | --- |
| ~10 RPS is reproducible on this laptop/harness | **High** |
| Pool wait is not the baseline bottleneck | **High** |
| Queue-position query is cheap up to ~1k queued | **High** |
| Unbounded N+1 is not present in profiled endpoints | **Medium-high** |
| Non-DB application/framework time > SQL at p50 | **Medium-high** |
| Absolute product capacity (dedicated API process, remote DB) | **Low** — not measured |
| Precise split of non_db into Pydantic vs middleware vs ORM | **Low** — needs tighter stage wraps |

---

## Recommended next experiment

1. **Split processes:** run API in a separate process (or machine) from the load
   generator; keep `request_profiling_enabled` and compare client vs
   `X-CF-Profile` again.
2. If split-process RPS remains ~10, deepen **application CPU** profiling
   (cProfile / py-spy on the API process only) focused on
   `submission_creation` and challenge read mapping.
3. Only after (1)–(2): consider a **single** change (e.g. remove redundant
   hackathon fetch if authorization can reuse already-loaded data) with a
   before/after under the same harness.

Do **not** introduce Redis, replicas, or pool enlargement until a split-process
baseline still shows a concrete A/B/C target.

---

## How to reproduce

```bash
# Clean baseline (~10 RPS)
python scripts/interactive_profile.py \
  --rate 10 --duration 8 --clients 8 \
  --datasets small --worker-modes 0,1 \
  --results-path docs/interactive-path-profile-baseline.json

# Pressure matrix
python scripts/interactive_profile.py \
  --rate 40 --duration 6 --clients 40 \
  --datasets small,realistic,large --worker-modes 0,1 \
  --results-path docs/interactive-path-profile-results.json
```

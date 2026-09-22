# Interactive path — isolated load-generator profile

Investigation only. No Redis, caching, indexes, pool growth, or replicas.

Raw data: `docs/interactive-isolated-load-profile-results.json`  
Same-process reference: `docs/interactive-path-profile-baseline.json` (prior commit)  
Harness: `scripts/interactive_isolated_profile.py`

---

## Environment

| Item | Value |
| --- | --- |
| Host | Windows laptop (API + load gen + optional workers share host) |
| Isolation | **API in separate OS process** (`spawn`); load generator = parent; evaluation workers = **third OS process** when enabled |
| Python | 3.11.0 |
| PostgreSQL | 16.2 embedded `pgserver` |
| DB pool | `pool_size=8`, `max_overflow=4` |
| Profiling | `request_profiling_enabled=True` → `X-CF-Profile` |
| Available RAM during run | ~0.6–1.0 GB free (~94% used) — host under memory pressure |

---

## Workload

Unchanged mix (equal rotation):

1. challenge read  
2. evaluation status  
3. participant history  
4. submission creation  

| Parameter | Value |
| --- | --- |
| Dataset | `small` (12 seeded submissions, drained) |
| Duration | 8 s per scenario |
| Clients | 8 at ≤10 RPS; else ≈ offered RPS (capped 100) |
| Rates | 10, 20, 40, 60, 80, 100 |
| Workers | 0 and 1 (third process) |
| Stop rule | saturated if timeouts/errors ≥5%, achieved &lt;70% offered, client/server p95 ≥2 s — **continue-on-saturation** used for the full matrix |

---

## Same-process vs isolated (10 RPS)

| Metric | A. Same-process (`workers=0`) | B. Isolated (`workers=0`) |
| --- | ---: | ---: |
| Offered RPS | 10 | 10 |
| Achieved RPS | **9.80** | **10.06** |
| Client p50 / p95 / p99 | 102 / 245 / 1388 ms | **28 / 57 / …** ms |
| Server p50 / p95 | 38 / 68 ms | **15 / 32** ms |
| Pool wait p50 | 3.8 ms | **1.4** ms |
| SQL p50 | 9.5 ms | **4.0** ms |
| non_db p50 | 22.8 ms | **9.3** ms |
| Errors / timeouts | 0 / 0 | 0 / 0 |
| Client CPU | ~74% (shared process) | **15%** |
| Server CPU | (same process) | **16%** |
| Server RSS | ~89 MB shared | **83 MB** |

**Conclusion:** the prior ~10 RPS ceiling was largely a **same-process / shared event-loop artifact**. With isolation, 10 RPS is easy and client↔server latency gap shrinks dramatically.

---

## Rate sweep (isolated)

### workers = 0

| Offered | Achieved | Client p50/p95 | Server p50/p95 | Pool p50 | SQL p50 | non_db p50 | Err | Cli CPU | Srv CPU | Saturated? |
| ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 10 | **10.06** | 28 / 57 | 15 / 32 | 1.4 | 4.0 | 9.3 | 0 | 15% | 16% | no |
| 20 | **19.99** | 28 / 58 | 14 / 29 | 1.6 | 3.5 | 9.0 | 0 | 26% | 30% | no |
| 40 | **39.94** | 35 / 91 | 17 / 44 | 1.8 | 4.8 | 9.7 | 0 | 49% | 53% | no |
| 60 | 9.13 | 2206 / 14044 | 28 / 223 | 3.1 | 7.1 | 17.2 | 1 | 75% | 25% | **yes** |
| 80 | 8.33 | 2714 / 12410 | 32 / 243 | 3.4 | 7.9 | 19.7 | 0 | 79% | 23% | **yes** |
| 100 | 6.92 | 3723 / 13304 | 37 / 252 | 4.0 | 9.2 | 22.3 | 1 | 82% | 23% | **yes** |

### workers = 1 (third process)

| Offered | Achieved | Client p50/p95 | Server p50/p95 | Err | Cli CPU | Srv CPU | Saturated? |
| ---: | ---: | --- | --- | ---: | ---: | ---: | --- |
| 10 | **10.08** | 41 / 57 | 19 / 30 | 0 | 20% | 21% | no |
| 20 | **19.96** | 24 / 50 | 12 / 23 | 0 | 25% | 25% | no |
| 40 | 14.94 | 1523 / 5397 | 24 / 106 | 0 | 88% | 35% | **yes** |
| 60–100 | 6–14 | seconds / seconds | p50 still tens of ms | 0–1 | 80–89% | 21–33% | **yes** |

Run-to-run note: an earlier isolated workers=0 pass sustained **~60 RPS** (client p95 ~69 ms) before host memory pressure worsened. The matrix above (available RAM often &lt;1 GB) is the recorded authoritative continue-on-saturation run. Treat **≥40 RPS** as the **confident** sustained isolated capacity on this laptop; **~60 RPS** as intermittently achievable under quieter host conditions.

---

## Client vs server latency

- Below saturation (≤40 RPS, workers=0): client p95 stays **&lt;100 ms**, server p95 **&lt;50 ms**.
- Above saturation: client latency explodes to **seconds** while server p50 remains **~25–40 ms**.
- That pattern means requests spend most wall time **queued outside the profiled API handler** (client/event scheduling / OS / accept backlog), not inside measured SQL.

---

## CPU / RSS

- Server RSS stable **~83–94 MB**.
- At healthy 40 RPS: API CPU ~53%, client ~49% — both busy but successful.
- At failed 60–100 RPS: **client CPU 75–88%** while **server CPU drops to ~22–35%** — the load generator / shared host is saturated; the API process is *not* pegged.

---

## DB pool / SQL / non-DB

Across healthy rates:

- Pool wait p50 **~1.4–1.8 ms** (lower than same-process).
- SQL p50 **~3.5–5 ms**.
- non_db p50 **~9–10 ms** (still the largest exclusive server bucket, but absolute cost is small at 40 RPS).

No evidence of pool exhaustion or SQL as the RPS ceiling up to 40.

Queue-position bench (post-drive, workers stopped) remains ~12 ms through depth 1000 (same as prior profile).

---

## Bottleneck classification

| Cause | Evidence |
| --- | --- |
| **Load generator / shared-host contention** | Dominant above ~40–60 RPS: client CPU high, server CPU low, client latency ≫ server profile |
| **Same-process event-loop coupling** | Explains prior ~10 RPS myth — removed by isolation → 4× headroom |
| **Application non-DB work** | Still largest *server* stage, but not limiting at 40 RPS |
| **Evaluation workers** | workers=1 fails earlier (40 RPS) than workers=0 on this host — shared CPU/DB with API under memory pressure |
| **Database SQL** | Not the ceiling (few ms) |
| **Connection pool** | Not the ceiling (~1–4 ms p50) |
| **Network/HTTP localhost** | Minor; client≈server×2 at healthy rates, not orders of magnitude |

**Label:** previously **load-generator artifact**; now **shared-laptop saturation** around offered 40–60 RPS, with server-side work still inexpensive when requests are actually handled.

---

## Is optimization justified?

**No product optimization yet.**

Isolation raised measured interactive capacity from ~10 RPS to **~40 RPS sustained** (workers=0) without code changes. That means earlier Redis/cache/pool proposals would have optimized the wrong problem.

Optimization becomes justified only after a quieter host or remote load generator still shows a clear server-side limit (e.g. API CPU pegged, pool wait dominant, or SQL dominant).

---

## Confidence / limitations

| Claim | Confidence |
| --- | --- |
| Same-process harness understated API capacity | **High** |
| Isolated API sustains ~40 RPS interactive mix on this laptop | **High** |
| Absolute production capacity | **Low** — embedded Postgres, laptop RAM pressure, localhost only |
| Exact cliff between 40 and 60 | **Medium** — host noise; one quieter run reached ~60 |

---

## Recommended next experiment

1. Re-run isolated sweep on a quieter machine / with more free RAM, load generator on a **second host** if possible.  
2. If capacity remains ~40–60 and server CPU stays low at the cliff → measure accept queue / uvicorn concurrency / single-worker asyncio scheduling.  
3. Only then consider a **single** targeted change (e.g. uvicorn workers=2 *as an experiment*, or trim redundant hackathon fetch) with before/after under the isolated harness.

Do **not** add Redis/indexes/pool enlargement first.

---

## Reproduce

```bash
python scripts/interactive_isolated_profile.py \
  --rates 10,20,40,60,80,100 \
  --duration 8 \
  --datasets small \
  --worker-modes 0,1 \
  --continue-on-saturation \
  --results-path docs/interactive-isolated-load-profile-results.json
```

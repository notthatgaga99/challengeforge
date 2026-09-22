# ChallengeForge

ChallengeForge is a competitive challenge/hackathon platform and an evolving
system-design experiment. It demonstrates engineering judgment through:

> Build → measure → break → understand → redesign → document.

The project remains a laptop-runnable **modular monolith**. Complexity is added
only when experiments expose a concrete failure mode—not to accumulate
technologies or decorate an architecture diagram.

## Current architecture

```text
Participants / organizers
          |
          v
  FastAPI + Jinja UI
   (interactive plane)
      |          \
      v           v
 PostgreSQL    local artifacts
      ^
      |
 evaluation worker process(es)
 durable queue + bounded concurrency
 FIFO + bounded LIGHT bypass
```

PostgreSQL is authoritative for business transitions and evaluation claiming.
Workers commit a claim before CPU work and write results in a separate
transaction. No Redis, Kafka, Celery, Kubernetes, or distributed scheduler is
required.

## Current capabilities

Participant:

- browse published hackathons and challenges
- create idempotent submissions and attach artifacts
- submit work and track queued/running/completed/failed evaluation
- see an honest approximate count of jobs ahead
- inspect submission history

Organizer:

- create, publish, and close hackathons/challenges
- define specifications and weighted criteria
- inspect submissions
- view queue health and LIGHT/MEDIUM/HEAVY backlog counts

System:

- database-authoritative concurrency transitions
- atomic submission acceptance and evaluation creation
- durable asynchronous evaluation with ownership checks
- stale-worker recovery and bounded retries
- FIFO scheduling with narrowly bounded LIGHT bypass
- explicit, environment-specific evaluation concurrency
- reproducible correctness, capacity, resource, scheduling, and interactive
  load experiments

There is **no real authentication** yet. Development identity uses
`X-User-Id` (and a UI cookie). AI assistance is also intentionally not
implemented.

## Engineering experiments

| Experiment | Observation | Decision |
|---|---|---|
| Submission concurrency | Application-only checks lost business races | PostgreSQL-authoritative conditional transitions and row locks |
| Async evaluation | Request-bound evaluation would couple latency and failures | Durable PostgreSQL jobs plus worker processes |
| Arrival vs service capacity | Backlog grows when arrivals exceed service | Keep PostgreSQL; expose queue health instead of rejecting durable work |
| Worker scaling | 2 workers peaked; 4–8 reduced useful throughput | Explicit bounded worker capacity |
| Heterogeneous FIFO | HEAVY work caused LIGHT head-of-line delay | At most two immediate LIGHT bypasses; no general scheduler |
| Interactive capacity | Attempted 100 RPS saturated near 10 RPS; sustained evaluation pressure worsened latency | Default to one evaluation worker for interactive headroom; profile baseline before adding infrastructure |

Latest measured statement:

> On the tested Windows laptop, under the documented mixed request workload
> and sustained evaluation backlog, one evaluation worker completed about
> 11.6 HTTP requests/sec at about 7.2 s p95 with 99.75% success. The attempted
> 100 requests/sec was **not** sustained.

## Run locally

Requires Python 3.11+, Docker, and Git.

```powershell
docker compose up -d
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
alembic upgrade head
uvicorn challengeforge.main:app --reload --app-dir src
```

If Docker Desktop cannot pull images, run without Compose:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python scripts/run_dev.py
```

Open http://localhost:8000 for the UI, or http://localhost:8000/docs for OpenAPI.

Seeded identities (also listed at `GET /api/v1/dev/users`):

| Role | Display name | `X-User-Id` |
|---|---|---|
| organizer | Ada Organizer | `11111111-1111-4111-8111-111111111111` |
| participant | Pat Participant | `22222222-2222-4222-8222-222222222222` |
| participant | Riley Participant | `33333333-3333-4333-8333-333333333333` |

## Tests

```powershell
pytest
```

Tests use Docker Postgres on port 5432 when it is already running. If Docker cannot pull images (for example Docker Desktop requires an organization login), they start an embedded PostgreSQL via `pgserver` instead. That is a test convenience, not a second production database.

To use Docker Postgres for both the app and tests (after signing in if your daemon requires it):

```powershell
docker compose up -d
$env:TEST_DATABASE_URL = "postgresql+asyncpg://challengeforge:challengeforge@localhost:5432/challengeforge_test"
pytest
```

## Interactive capacity experiment

The bounded harness starts an isolated PostgreSQL and application server, then
drives configurable mixed HTTP traffic while evaluation capacity is idle or
backlogged:

```powershell
python scripts/interactive_capacity_experiment.py --rate 100 --duration 8 --clients 100
```

See [the report](docs/interactive-capacity-report.md) for the workload model,
measured limitations, and exact capacity statement.

## Concurrency experiment

The invariant-focused experiment starts the current application and an isolated
PostgreSQL, then runs duplicate, independent-write, read/write, close-race,
lost-response, and state-transition scenarios:

```powershell
python scripts/concurrency_experiment.py
```

The selected database is dropped and recreated. With no arguments the script
uses an isolated embedded PostgreSQL. Set `EXPERIMENT_DATABASE_URL` only to a
disposable PostgreSQL database.

Results and analysis:

- [docs/concurrency-report.md](docs/concurrency-report.md)
- [docs/concurrency-results.json](docs/concurrency-results.json)

The follow-up correctness design and adversarial verification are separate so
the failure baseline remains historical:

```powershell
python scripts/correctness_experiment.py
```

- [docs/concurrency-design.md](docs/concurrency-design.md)
- [docs/concurrency-correctness-results.json](docs/concurrency-correctness-results.json)
- [docs/adr/0002-database-authoritative-concurrency.md](docs/adr/0002-database-authoritative-concurrency.md)

## Evaluation pipeline

Accepted submissions enqueue a durable evaluation (`QUEUED` → `RUNNING` →
`SUCCEEDED` / `FAILED`). PostgreSQL serializes the short claim decision,
enforces explicit capacity, and applies FIFO plus bounded LIGHT bypass. Run a
worker:

```powershell
python -m challengeforge.worker --worker-id worker-a
```

Adversarial experiments A–F:

```powershell
python scripts/evaluation_experiment.py
```

## Capacity / resource experiments

```powershell
python scripts/capacity_experiment.py
python scripts/resource_capacity_experiment.py
python scripts/evaluation_scheduling_experiment.py
```

- [docs/evaluation-pipeline.md](docs/evaluation-pipeline.md)
- [docs/evaluation-pipeline-results.json](docs/evaluation-pipeline-results.json)
- [docs/evaluation-capacity-report.md](docs/evaluation-capacity-report.md)
- [docs/evaluation-capacity-results.json](docs/evaluation-capacity-results.json)
- [docs/resource-capacity-report.md](docs/resource-capacity-report.md)
- [docs/resource-capacity-results.json](docs/resource-capacity-results.json)
- [docs/evaluation-scheduling.md](docs/evaluation-scheduling.md)
- [docs/evaluation-scheduling-results.json](docs/evaluation-scheduling-results.json)
- [docs/interactive-capacity-report.md](docs/interactive-capacity-report.md)
- [docs/interactive-capacity-results.json](docs/interactive-capacity-results.json)
- [docs/interactive-capacity-isolation-results.json](docs/interactive-capacity-isolation-results.json)
- [docs/adr/0003-async-evaluation-boundary.md](docs/adr/0003-async-evaluation-boundary.md)
- [docs/adr/0004-keep-postgresql-evaluation-queue.md](docs/adr/0004-keep-postgresql-evaluation-queue.md)

See [docs/architecture.md](docs/architecture.md) and [docs/adr/0001-modular-monolith.md](docs/adr/0001-modular-monolith.md).

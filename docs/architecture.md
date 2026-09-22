# ChallengeForge architecture (slice 1)

## 1. System context

ChallengeForge is an internal-style platform for running a technical hackathon: organizers publish challenges, participants submit work, and (later) an assistant can help against the specification.

This version is designed around one event on a laptop:

- ~500 registered participants, ~150–200 concurrently active
- ~10–20 requests/sec typical, ~50 requests/sec short bursts
- Challenge browsing, challenge retrieval, submission create, submission status

It is **not** sized as a distributed system. Postgres is the only extra
required store. Evaluation workers are optional sibling processes of the same
modular monolith (same code, same database)—not microservices.

```text
Browser (minimal UI)
    |
    v
FastAPI process  (/api/v1 + server-rendered pages)
    |
    +--> PostgreSQL  <--+-- evaluation worker process(es)
    +--> Local filesystem (artifacts)
```

## 2. Major components

| Component | Responsibility |
|---|---|
| `api` | HTTP adapters, request/response schemas, error mapping, request IDs |
| `web` | Thin Jinja UI that calls the same `/api/v1` contract |
| `application` | Use cases and transaction boundaries |
| `domain` | Entities, roles, submission/evaluation state machines |
| `persistence` | SQLAlchemy models and repositories; maps to domain objects |
| `storage` | `ArtifactStorage` protocol + filesystem implementation |
| `identity` | Development `CurrentUser` resolution (replaceable) |
| `worker` | Polls/claims/completes evaluation jobs via PostgreSQL |

ORM rows are not API models. Route handlers never return `*Row` objects.

## 3. Domain model

- **User** — organizer or participant
- **Hackathon** — draft / published / closed; owned by the creating organizer
- **Challenge** — belongs to a hackathon; draft / published / closed
- **ChallengeSpecification** — 1:1 text body (future retrieval source)
- **EvaluationCriterion** — named, weighted criteria
- **Submission** — belongs to a participant and a challenge
- **Evaluation** — 1:1 with a submitted submission; async scoring job

Submission state machine (not a workflow engine):

```text
CREATED --> SUBMITTED
CREATED --> CANCELLED
CREATED --> FAILED
```

`SUBMITTED`, `CANCELLED`, and `FAILED` are terminal for the submission itself.
On `SUBMITTED`, an **Evaluation** is created `QUEUED` in the same transaction.
Workers then drive `QUEUED → RUNNING → SUCCEEDED|FAILED`. See
[`evaluation-pipeline.md`](evaluation-pipeline.md).

Participants may have **many** submissions per challenge (history). Duplicate *attempts* from retries are a different problem; see idempotency below.

## 4. API boundaries

Versioned under `/api/v1`. Resources:

- Hackathons are the top-level event. Challenges are created under a hackathon so ownership is obvious, and fetched by challenge id because that is the hot browse path.
- Submissions are created under a challenge. Organizers list by challenge; participants list by user.
- Organizers close a published challenge with `POST /challenges/{id}/close`.

Identity is **not** mixed into resource URLs except `GET /users/{id}/submissions`. The actor is `X-User-Id` today; a future auth middleware should still produce `CurrentUser`.

Idempotency is applied **only** to submission create (`Idempotency-Key`).

Assumption: retries and double-clicks cluster on “submit”, not on “create hackathon”.  
Decision: persist `(participant_id, idempotency_key)` with a partial unique index and replay the original row.  
The key remains participant-wide. A SHA-256 fingerprint of canonical
`challenge_id` plus metadata distinguishes an actual retry from key reuse with
different input; mismatches return 409.  
Trade-off: clients must send a key if they want safe retries; omitting the key always creates a new attempt.
Reconsider if we observe accidental duplicate attempts in the UI despite keys, or if product wants a single submission per participant.

## 5. Persistence model

PostgreSQL 16 in [`docker-compose.yml`](../docker-compose.yml) is the intended runtime database. Tests will use that instance when port 5432 is already up; otherwise they start an embedded Postgres (`pgserver`) so a locked Docker Hub login cannot block verification. Embedded Postgres is a test convenience, not a second production architecture.

Indexes that match access:

- `challenges(hackathon_id)`
- `submissions(participant_id, created_at)`
- `submissions(challenge_id, created_at)`
- unique `(participant_id, idempotency_key)` where key is present
- unique `evaluations(submission_id)` — one evaluation per submission
- `evaluations(status, created_at)` — worker claim ordering
- `evaluations(status, started_at)` — stale RUNNING recovery
Check constraints pin role/status vocabularies. Alembic is the schema source for running systems; tests build the same SQLAlchemy metadata.

Connection pool: `pool_size=5`, `max_overflow=5`. That is enough for 10–20 rps of short requests. Reconsider when pool checkout wait or p95 latency rises under the planned load script.

Each use case commits once (challenge + specification + criteria is one transaction).

Concurrency authority:

- terminal submission transitions are conditional database updates requiring
  current status `created`;
- submission acceptance holds a shared challenge-row lock through insert and
  commit;
- challenge close holds the conflicting exclusive row lock.

See [`concurrency-design.md`](concurrency-design.md) and
[`adr/0002-database-authoritative-concurrency.md`](adr/0002-database-authoritative-concurrency.md).

## 6. Artifact-storage boundary

Submission bytes are not stored in PostgreSQL. `ArtifactStorage.put/get/exists` is the only storage API the application uses. Slice 1 writes under `./data/artifacts`.

Write order: persist the file, then the row. Orphan files are acceptable; a row pointing at a missing object is not. Reconsider if orphan volume becomes measurable.

Replacing the filesystem with object storage later should not change submission use cases.

## 7. Current capacity assumptions

| Signal | Assumption |
|---|---|
| Concurrent users | 150–200 |
| Sustained rps | 10–20 |
| Burst rps | ~50 |
| Process | 1 uvicorn worker |
| Cache | none |
| Queue | none |

These numbers fit a single app process and a small Postgres. We will be wrong in a useful way if we measure otherwise.

## 8. Important failure modes

- Duplicate submit click / lost response → mitigated by `Idempotency-Key` on create; a second *intentional* attempt must use a new key.
- Invalid state transition → `409 invalid_state_transition`.
- Missing/unknown identity → `401`; wrong role → `403`.
- Unique/FK violations → mapped to 409 or 404, not 500, when we catch them; unexpected integrity errors still 500 with a request id.
- Artifact put failure before insert → no submission row (client retries).
- Unexpected exceptions → `500` with `request_id`, no stack trace in the body; JSON logs include the exception.

## 9. Deliberate non-decisions

Not in this slice, on purpose:

- Real authentication, sessions, SSO
- Redis, Kafka, Kubernetes, extra app services
- Object storage, CDN
- Background workers / evaluation pipeline
- LLM, retrieval, tools, or an “Agent” type
- Vector database
- Horizontal scaling, read replicas, in-memory caches
- A product-grade SPA

AI later: a Challenge Assistant would be a new application service that reads `ChallengeSpecification` and submission evidence (`metadata` + `artifact_key`). It should not own the submission state machine.

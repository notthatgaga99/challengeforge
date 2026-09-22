# ADR 0001: Modular monolith first

## Status

Accepted for ChallengeForge slice 1.

## Context

We need a working hackathon/challenge platform that can run on a single laptop, survive ~150–200 concurrent users and ~10–20 rps (50 rps burst), and still leave room to grow.

Microservices, a message bus, and an orchestration platform would all “look like” a serious architecture. They would also multiply processes, failure modes, and local resource use before we have a workload that requires them.

## Decision

Ship a **modular monolith**: one deployable FastAPI process with in-process module boundaries (API, application, domain, persistence, artifact storage, identity). Persist to one PostgreSQL. Store blobs behind an interface implemented with the local filesystem.

This is not a claim that modular monoliths are universally superior. It is a fit for this product at this load on this hardware.

## Why this, now

**Local resource constraints.** A laptop should run Postgres + one app process. Extra brokers, sidecars, and service meshes spend RAM/CPU on ceremony.

**Development complexity.** One process means one debugger, one log stream, one migration history. Module boundaries still keep domain logic from leaking into handlers.

**Deployment simplicity.** `docker compose up` for Postgres, `uvicorn` for the app, `alembic upgrade`. That matches how we will actually run and break the system in the next iterations.

**Current workload.** Hundreds of users and tens of requests per second are a database-and-app problem, not a distributed-systems problem. We would use a queue when evaluation or AI work becomes slow or asynchronous, not because submissions exist.

**Future extraction boundaries.** If a module later needs independent scale or a different runtime, the seams already exist: HTTP API vs application vs persistence vs `ArtifactStorage` vs identity. Extraction is a response to a measured bottleneck, not a starting shape.

## Consequences

- Operational simplicity and fast iteration.
- Weaker isolation: a leaky module or a blocked event loop can affect the whole app.
- We must keep boundaries honest (no ORM in the API contract, no filesystem calls in domain code) or extraction later will be painful.

## Reconsider when

- A module needs a different scaling profile (e.g. AI inference vs CRUD).
- We must isolate failure (evaluation jobs taking down browse traffic).
- More than one team independently releases parts of the system.
- Load tests show the single process or single database is the constraint, *after* we have measured it.

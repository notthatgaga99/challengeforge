# Search tenancy / authz design

**Status:** KEEP — deny-by-default effective SQL scope  
**ADR:** `docs/adr/0015-search-tenancy-authz.md`

## 1. Problem

Lexical/dense retrieval joins `document_chunks` → `ingestion_jobs` only.
Optional `RetrievalFilters` (`submission_id`, `artifact_key`, `ingestion_job_id`)
do not encode hackathon/challenge/actor scope. A product search API would leak
chunks across tenants.

## 2. Industry options

| Approach | Pros | Cons | Verdict |
|---|---|---|---|
| Filter hits in application after retrieve | Simple | Easy to forget; over-fetches secrets into memory | Reject |
| Postgres row-level security | DB-enforced | Opaque to app tests; heavy for curriculum | Defer |
| **Deny-by-default effective filters in SQL** | Same path as FTS/dense; testable; monolith-fit | Needs joins to submissions/challenges | **KEEP** |

## 3. Decision

1. Extend `RetrievalFilters` with `hackathon_id`, `challenge_id`, `participant_id`.
2. Product path: `resolve_retrieval_scope(user, hackathon_id, …) → RetrievalFilters`.
   Client hints cannot widen scope.
3. Policy:
   - **Organizer:** `hackathon_id` they own; optional `challenge_id` in that hackathon;
     sees all succeeded-job chunks in scope.
   - **Participant:** `hackathon_id` required (hackathon must be published or closed);
     effective `participant_id = actor.id` always.
   - Wrong tenant / missing scope → `PermissionDenied` (403).
4. SQL: join `document_chunks` → `submissions` → `challenges` when tenancy filters set.
5. API: `POST /api/v1/hackathons/{hackathon_id}/search`.
6. Offline experiments may call `retrieve(..., filters=…)` without authz; **API is authz-mandatory**.

## 4. Non-goals

SSO, RLS policies, Redis ACL caches, promoting hybrid as live default.

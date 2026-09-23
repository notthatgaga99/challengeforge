# ChallengeForge engineering curriculum record

Rolling log of fast-build curriculum milestones.  
**Built ≠ Understood ≠ Can design ≠ Can defend in an interview.**

---

## Chunking & Representation (current)

| Field | Entry |
|---|---|
| Decision | KEEP hybrid structure-aware chunking + char budget; Postgres `document_chunks` |
| Concepts learned | Canonical vs raw offsets; versioned chunk sets; span-exact provenance; why token budgets wait |
| Alternatives understood | Fixed / token / structure-only / recursive / hybrid |
| Experiment | `scripts/chunking_representation_experiment.py` → `docs/chunking-and-representation-results.json` |
| What failed | Pack-by-join broke `content == canonical[start:end]`; fixed via contiguous spans |
| Evidence proves | Deterministic bounded chunks + durable complete-set semantics on laptop |
| Does not prove | Retrieval / embedding / RAG quality |
| Can explain | Durability boundary for chunk sets; identity under re-ingest; overlap deferred |
| Still cannot explain confidently | Best size/overlap for a specific embedding model; production symbol chunking |
| Evidence in repo | ADR 0011, `docs/chunking-and-representation.md`, tests, migration 0012 |
| Interview questions prepared | How do you version chunks when the parser changes? How do you prevent partial chunk sets from looking complete? Why not tokens yet? |
| Reconsideration trigger | Retrieval boundary failures; embedding model constraints; AST needs; scale |

---

## Prior milestones (pointers)

| Milestone | Decision | Doc |
|---|---|---|
| Ingestion pipeline reliability | KEEP Postgres durable jobs | `docs/ingestion-pipeline-reliability.md` / ADR 0010 |
| Large uploads | KEEP stream + finalize | `docs/large-upload-architecture.md` |
| Artifact consistency | BLOB-FIRST + compensate + reconcile | `docs/artifact-storage-consistency.md` / ADR 0009 |
| Isolation boundary | KEEP subprocess for trusted synthetic only | `docs/execution-isolation-boundary.md` / ADR 0008 |

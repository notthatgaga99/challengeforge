"""Lexical retrieval baseline experiment.

Loads docs/lexical-retrieval-corpus.json, ingests via chunker into Postgres,
evaluates cf-lex-fts-1 / cf-lex-fts-simple-1 / cf-lex-fts-struct-1, and measures
latency on synthetic corpora up to 100k chunks (laptop evidence only).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from challengeforge.domain.enums import (  # noqa: E402
    ChallengeStatus,
    HackathonStatus,
    SubmissionStatus,
)
from challengeforge.identity import ORGANIZER_ID, PARTICIPANT_ID  # noqa: E402
from challengeforge.ingestion.canonical import normalize_to_canonical  # noqa: E402
from challengeforge.ingestion.chunking import (  # noqa: E402
    CHUNKER_VERSION,
    chunk_hybrid,
)
from challengeforge.ingestion.canonical import PARSER_VERSION  # noqa: E402
from challengeforge.persistence.mapping import utcnow  # noqa: E402
from challengeforge.persistence.models import (  # noqa: E402
    Base,
    ChallengeRow,
    HackathonRow,
    IngestionJobRow,
    SubmissionRow,
)
from challengeforge.persistence.repositories import DocumentChunkRepository  # noqa: E402
from challengeforge.persistence.seed import seed_dev_users  # noqa: E402
from challengeforge.retrieval.lexical import (  # noqa: E402
    RETRIEVAL_VERSION_FTS,
    RETRIEVAL_VERSION_SIMPLE,
    RETRIEVAL_VERSION_STRUCT,
    retrieve,
)
from challengeforge.retrieval.metrics import evaluate_query, macro_average  # noqa: E402
from challengeforge.retrieval.schema import ensure_lexical_search_schema  # noqa: E402


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_corpus() -> dict:
    path = ROOT / "docs" / "lexical-retrieval-corpus.json"
    return json.loads(path.read_text(encoding="utf-8"))


async def setup_engine(database_url: str):
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await ensure_lexical_search_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def ingest_documents(factory, documents: list[dict]) -> dict[str, list]:
    """Ingest corpus docs; return doc_id -> list[DocumentChunk]."""
    mapping: dict[str, list] = {}
    async with factory() as session:
        await seed_dev_users(session)
        now = utcnow()
        hack = HackathonRow(
            id=uuid4(),
            title="LexEval",
            description="d",
            status=HackathonStatus.PUBLISHED.value,
            organizer_id=ORGANIZER_ID,
            created_at=now,
            updated_at=now,
        )
        session.add(hack)
        chal = ChallengeRow(
            id=uuid4(),
            hackathon_id=hack.id,
            title="C",
            description="d",
            constraints="",
            status=ChallengeStatus.PUBLISHED.value,
            created_at=now,
            updated_at=now,
        )
        session.add(chal)
        await session.flush()

        for doc in documents:
            sub = SubmissionRow(
                id=uuid4(),
                challenge_id=chal.id,
                participant_id=PARTICIPANT_ID,
                status=SubmissionStatus.CREATED.value,
                metadata_json={"corpus_doc_id": doc["id"]},
                created_at=now,
                updated_at=now,
            )
            session.add(sub)
            await session.flush()
            job_id = uuid4()
            key = f"corpus/{doc['id']}.md"
            job = IngestionJobRow(
                id=job_id,
                submission_id=sub.id,
                artifact_key=key,
                status="succeeded",
                attempt_count=1,
                worker_id="lex-exp",
                available_at=now,
                started_at=now,
                completed_at=now,
                error_code=None,
                error_message=None,
                result_key=f"ingestion/{job_id}/result.json",
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            await session.flush()
            canonical = normalize_to_canonical(doc["text"])
            drafts = chunk_hybrid(canonical, max_chars=1200)
            chunks = await DocumentChunkRepository(session).replace_for_job(
                ingestion_job_id=job_id,
                submission_id=sub.id,
                artifact_key=key,
                parser_version=PARSER_VERSION,
                chunker_version=CHUNKER_VERSION,
                chunks=drafts,
            )
            mapping[doc["id"]] = chunks
        await session.commit()
    return mapping


def resolve_relevant(query: dict, doc_chunks: dict[str, list]) -> set[str]:
    relevant: set[str] = set()
    for sel in query.get("relevant_selectors", []):
        doc_id = sel["doc_id"]
        needle = sel.get("contains", "")
        for ch in doc_chunks.get(doc_id, []):
            if needle in ch.content:
                relevant.add(str(ch.id))
    return relevant


def classify_failure(
    *,
    query: dict,
    metrics,
    ranked_contents: list[str],
) -> str | None:
    if query.get("allow_empty_relevant") and not query.get("relevant_selectors"):
        if metrics.zero_hit:
            return None  # expected empty
        return "noise"
    if not metrics.zero_hit and metrics.recall_at.get(5, 0) >= 1.0:
        return None
    if query.get("expect_lexical_fail") and query.get("type") == "semantic_paraphrase":
        return "semantic_gap"
    if metrics.zero_hit:
        # Check if any ranked content shares vocabulary — else vocabulary mismatch
        q_terms = set(query["query"].lower().split())
        if any(q_terms & set(c.lower().split()) for c in ranked_contents):
            return "ranking_failure"
        return "vocabulary_mismatch"
    if metrics.first_relevant_rank and metrics.first_relevant_rank > 5:
        return "ranking_failure"
    if metrics.recall_at.get(10, 0) < 1.0:
        return "ranking_failure"
    return "partial"


async def run_eval(factory, corpus: dict, doc_chunks: dict[str, list]) -> dict:
    versions = [
        RETRIEVAL_VERSION_FTS,
        RETRIEVAL_VERSION_SIMPLE,
        RETRIEVAL_VERSION_STRUCT,
    ]
    ks = (1, 3, 5, 10)
    out: dict = {"versions": {}, "per_query": {}}
    for version in versions:
        metrics_list = []
        per_query = []
        failures: dict[str, int] = {}
        async with factory() as session:
            for q in corpus["queries"]:
                relevant = resolve_relevant(q, doc_chunks)
                t0 = time.perf_counter()
                hits = await retrieve(
                    session, q["query"], top_k=10, retrieval_version=version
                )
                latency_ms = (time.perf_counter() - t0) * 1000
                ranked_ids = [str(h.chunk_id) for h in hits]
                if q.get("allow_empty_relevant") and not relevant:
                    # Metrics undefined for empty R — track zero-hit separately
                    entry = {
                        "query_id": q["id"],
                        "type": q["type"],
                        "latency_ms": round(latency_ms, 3),
                        "n_hits": len(hits),
                        "zero_hit": len(hits) == 0,
                        "expect_lexical_fail": q.get("expect_lexical_fail", False),
                        "skipped_metrics": True,
                    }
                    per_query.append(entry)
                    continue
                if not relevant:
                    continue
                m = evaluate_query(
                    query_id=q["id"],
                    ranked_ids=ranked_ids,
                    relevant=relevant,
                    ks=ks,
                )
                metrics_list.append(m)
                fail = classify_failure(
                    query=q,
                    metrics=m,
                    ranked_contents=[h.content for h in hits],
                )
                if fail:
                    failures[fail] = failures.get(fail, 0) + 1
                per_query.append(
                    {
                        "query_id": q["id"],
                        "type": q["type"],
                        "latency_ms": round(latency_ms, 3),
                        "n_hits": len(hits),
                        "recall_at": m.recall_at,
                        "precision_at": m.precision_at,
                        "mrr": m.mrr,
                        "first_relevant_rank": m.first_relevant_rank,
                        "zero_hit": m.zero_hit,
                        "expect_lexical_fail": q.get("expect_lexical_fail", False),
                        "failure_category": fail,
                        "relevant_count": len(relevant),
                    }
                )
        out["versions"][version] = {
            "aggregate": macro_average(metrics_list, ks),
            "failure_counts": failures,
            "queries": per_query,
        }
        out["per_query"][version] = per_query
    return out


async def seed_synthetic(factory, n_chunks: int) -> None:
    """Insert N searchable chunks efficiently (perf harness, not product path)."""
    from challengeforge.persistence.models import DocumentChunkRow
    import hashlib

    async with factory() as session:
        await seed_dev_users(session)
        now = utcnow()
        hack = HackathonRow(
            id=uuid4(),
            title="Perf",
            description="d",
            status=HackathonStatus.PUBLISHED.value,
            organizer_id=ORGANIZER_ID,
            created_at=now,
            updated_at=now,
        )
        session.add(hack)
        chal = ChallengeRow(
            id=uuid4(),
            hackathon_id=hack.id,
            title="C",
            description="d",
            constraints="",
            status=ChallengeStatus.PUBLISHED.value,
            created_at=now,
            updated_at=now,
        )
        session.add(chal)
        await session.flush()
        batch = 500
        for start in range(0, n_chunks, batch):
            end = min(n_chunks, start + batch)
            sub = SubmissionRow(
                id=uuid4(),
                challenge_id=chal.id,
                participant_id=PARTICIPANT_ID,
                status=SubmissionStatus.CREATED.value,
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
            session.add(sub)
            await session.flush()
            job_id = uuid4()
            session.add(
                IngestionJobRow(
                    id=job_id,
                    submission_id=sub.id,
                    artifact_key=f"perf/{start}.txt",
                    status="succeeded",
                    attempt_count=1,
                    worker_id="perf",
                    available_at=now,
                    started_at=now,
                    completed_at=now,
                    error_code=None,
                    error_message=None,
                    result_key=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            rows = []
            for i in range(start, end):
                content = (
                    f"Topic {i} evaluation scheduling and retries "
                    f"token{i % 97} worker queue durable claim."
                )
                rows.append(
                    DocumentChunkRow(
                        id=uuid4(),
                        ingestion_job_id=job_id,
                        submission_id=sub.id,
                        artifact_key=f"perf/{start}.txt",
                        parser_version=PARSER_VERSION,
                        chunker_version=CHUNKER_VERSION,
                        ordinal=i - start,
                        content=content,
                        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                        char_start=0,
                        char_end=len(content),
                        line_start=1,
                        line_end=1,
                        block_type="paragraph",
                        heading_path=["Perf", f"Topic {i}"],
                        oversized_split=False,
                        created_at=now,
                    )
                )
            session.add_all(rows)
            await session.flush()
        await session.commit()


async def run_perf(database_url: str, sizes: list[int]) -> list[dict]:
    rows = []
    for n in sizes:
        engine, factory = await setup_engine(database_url)
        await seed_synthetic(factory, n)
        latencies = []
        async with factory() as session:
            for _ in range(30):
                t0 = time.perf_counter()
                await retrieve(
                    session,
                    "evaluation scheduling retries",
                    top_k=10,
                    retrieval_version=RETRIEVAL_VERSION_FTS,
                )
                latencies.append((time.perf_counter() - t0) * 1000)
        latencies.sort()

        def pct(p: float) -> float:
            idx = min(len(latencies) - 1, int(round((p / 100) * (len(latencies) - 1))))
            return round(latencies[idx], 3)

        rows.append(
            {
                "n_chunks_target": n,
                "samples": len(latencies),
                "p50_ms": pct(50),
                "p95_ms": pct(95),
                "p99_ms": pct(99),
                "mean_ms": round(statistics.mean(latencies), 3),
            }
        )
        print(f"perf n~{n} {rows[-1]}", flush=True)
        await engine.dispose()
    return rows


async def run(args) -> dict:
    started_at = utc_iso()
    database_url = os.environ.get(
        "EXPERIMENT_DATABASE_URL",
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+asyncpg://challengeforge:challengeforge@localhost:5432/challengeforge_test",
        ),
    )

    def port_open():
        try:
            import socket

            with socket.create_connection(("127.0.0.1", 5432), timeout=0.3):
                return True
        except OSError:
            return False

    if not os.environ.get("TEST_DATABASE_URL") and not os.environ.get(
        "EXPERIMENT_DATABASE_URL"
    ):
        if not port_open():
            from pgserver import get_server

            data = ROOT / ".pgserver-lex-exp"
            data.mkdir(exist_ok=True)
            srv = get_server(str(data))
            database_url = srv.get_uri()
            if database_url.startswith("postgresql://"):
                database_url = "postgresql+asyncpg://" + database_url.removeprefix(
                    "postgresql://"
                )

    corpus = load_corpus()
    engine, factory = await setup_engine(database_url)
    doc_chunks = await ingest_documents(factory, corpus["documents"])
    eval_out = await run_eval(factory, corpus, doc_chunks)
    await engine.dispose()

    sizes = [100, 1000, 10000, 100000]
    if args.skip_100k:
        sizes = [100, 1000, 10000]
    perf = await run_perf(database_url, sizes)

    fts = eval_out["versions"][RETRIEVAL_VERSION_FTS]["aggregate"]
    paraphrase_fails = sum(
        1
        for q in eval_out["versions"][RETRIEVAL_VERSION_FTS]["queries"]
        if q.get("type") == "semantic_paraphrase" and q.get("zero_hit")
    )

    results = {
        "metadata": {
            "started_at": started_at,
            "completed_at": None,
            "database": "local",
            "environment": "laptop",
        },
        "corpus": {
            "documents": len(corpus["documents"]),
            "queries": len(corpus["queries"]),
            "path": "docs/lexical-retrieval-corpus.json",
        },
        "evaluation": eval_out,
        "performance": perf,
        "decision": {
            "gate": "KEEP PostgreSQL lexical FTS baseline (ts_rank_cd, not BM25)",
            "default_version": RETRIEVAL_VERSION_FTS,
            "structural_variant": RETRIEVAL_VERSION_STRUCT,
            "simple_variant": RETRIEVAL_VERSION_SIMPLE,
            "rationale": (
                "Measured Recall/MRR on a checked-in corpus; paraphrase queries "
                "expose semantic gaps that motivate embeddings next — without "
                "claiming vectors are proven better yet."
            ),
            "fts_recall_at_5": fts.get("recall_at", {}).get(5),
            "fts_mrr": fts.get("mrr"),
            "paraphrase_zero_hits": paraphrase_fails,
        },
    }
    results["metadata"]["completed_at"] = utc_iso()
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--results-path", default="docs/lexical-retrieval-results.json"
    )
    p.add_argument(
        "--skip-100k",
        action="store_true",
        help="Skip the ~100k chunk latency cell.",
    )
    args = p.parse_args()
    output = asyncio.run(run(args))
    path = ROOT / args.results_path
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(f"decision={output['decision']['gate']}")


if __name__ == "__main__":
    main()

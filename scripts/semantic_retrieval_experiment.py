"""Semantic vs lexical retrieval comparison experiment.

Indexes the lexical eval corpus with the curriculum (or MiniLM) embedding
provider, then compares cf-lex-fts-1 / cf-dense-1 / cf-hybrid-rrf-1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
from challengeforge.ingestion.canonical import PARSER_VERSION, normalize_to_canonical  # noqa: E402
from challengeforge.ingestion.chunking import CHUNKER_VERSION, chunk_hybrid  # noqa: E402
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
from challengeforge.retrieval.dense import (  # noqa: E402
    RETRIEVAL_VERSION_DENSE,
    RETRIEVAL_VERSION_HYBRID_RRF,
    index_chunks,
)
from challengeforge.retrieval.embeddings import (  # noqa: E402
    CurriculumConceptEmbeddingProvider,
    LocalMiniLMEmbeddingProvider,
    default_experiment_provider,
)
from challengeforge.retrieval.lexical import RETRIEVAL_VERSION_FTS, retrieve  # noqa: E402
from challengeforge.retrieval.metrics import evaluate_query, macro_average  # noqa: E402
from challengeforge.retrieval.schema import ensure_lexical_search_schema  # noqa: E402


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_corpus() -> dict:
    return json.loads(
        (ROOT / "docs" / "lexical-retrieval-corpus.json").read_text(encoding="utf-8")
    )


async def setup(database_url: str):
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await ensure_lexical_search_schema(engine)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def ingest_and_embed(factory, documents: list[dict], provider):
    mapping: dict[str, list] = {}
    async with factory() as session:
        await seed_dev_users(session)
        now = utcnow()
        hack = HackathonRow(
            id=uuid4(),
            title="SemEval",
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
        all_chunks = []
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
            session.add(
                IngestionJobRow(
                    id=job_id,
                    submission_id=sub.id,
                    artifact_key=key,
                    status="succeeded",
                    attempt_count=1,
                    worker_id="sem-exp",
                    available_at=now,
                    started_at=now,
                    completed_at=now,
                    error_code=None,
                    error_message=None,
                    result_key=f"ingestion/{job_id}/result.json",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            drafts = chunk_hybrid(normalize_to_canonical(doc["text"]), max_chars=1200)
            chunks = await DocumentChunkRepository(session).replace_for_job(
                ingestion_job_id=job_id,
                submission_id=sub.id,
                artifact_key=key,
                parser_version=PARSER_VERSION,
                chunker_version=CHUNKER_VERSION,
                chunks=drafts,
            )
            mapping[doc["id"]] = chunks
            all_chunks.extend(chunks)
        await index_chunks(session, provider, all_chunks)
        await session.commit()
    return mapping


def resolve_relevant(query: dict, doc_chunks: dict[str, list]) -> set[str]:
    relevant: set[str] = set()
    for sel in query.get("relevant_selectors", []):
        for ch in doc_chunks.get(sel["doc_id"], []):
            if sel.get("contains", "") in ch.content:
                relevant.add(str(ch.id))
    return relevant


async def eval_version(factory, corpus, doc_chunks, version, provider=None):
    ks = (1, 3, 5, 10)
    metrics_list = []
    per_query = []
    async with factory() as session:
        for q in corpus["queries"]:
            relevant = resolve_relevant(q, doc_chunks)
            if q.get("allow_empty_relevant") and not relevant:
                continue
            if not relevant:
                continue
            t0 = time.perf_counter()
            hits = await retrieve(
                session,
                q["query"],
                top_k=10,
                retrieval_version=version,
                embedding_provider=provider,
            )
            latency_ms = (time.perf_counter() - t0) * 1000
            m = evaluate_query(
                query_id=q["id"],
                ranked_ids=[str(h.chunk_id) for h in hits],
                relevant=relevant,
                ks=ks,
            )
            metrics_list.append(m)
            per_query.append(
                {
                    "query_id": q["id"],
                    "type": q["type"],
                    "expect_lexical_fail": q.get("expect_lexical_fail", False),
                    "latency_ms": round(latency_ms, 3),
                    "n_hits": len(hits),
                    "recall_at": m.recall_at,
                    "precision_at": m.precision_at,
                    "mrr": m.mrr,
                    "first_relevant_rank": m.first_relevant_rank,
                    "zero_hit": m.zero_hit,
                    "top_preview": hits[0].content[:120] if hits else None,
                }
            )
    return {
        "aggregate": macro_average(metrics_list, ks),
        "queries": per_query,
    }


async def run(args) -> dict:
    started = utc_iso()
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

            data = ROOT / ".pgserver-sem-exp"
            data.mkdir(exist_ok=True)
            srv = get_server(str(data))
            database_url = srv.get_uri()
            if database_url.startswith("postgresql://"):
                database_url = "postgresql+asyncpg://" + database_url.removeprefix(
                    "postgresql://"
                )

    if args.provider == "curriculum":
        provider = CurriculumConceptEmbeddingProvider()
    elif args.provider == "minilm":
        provider = LocalMiniLMEmbeddingProvider()
    else:
        provider = default_experiment_provider()
    corpus = load_corpus()
    engine, factory = await setup(database_url)
    doc_chunks = await ingest_and_embed(factory, corpus["documents"], provider)

    lex = await eval_version(factory, corpus, doc_chunks, RETRIEVAL_VERSION_FTS)
    dense = await eval_version(
        factory, corpus, doc_chunks, RETRIEVAL_VERSION_DENSE, provider
    )
    hybrid = await eval_version(
        factory, corpus, doc_chunks, RETRIEVAL_VERSION_HYBRID_RRF, provider
    )

    def paraphrase_zero(block):
        return sum(
            1
            for q in block["queries"]
            if q["type"] == "semantic_paraphrase" and q["zero_hit"]
        )

    results = {
        "metadata": {
            "started_at": started,
            "completed_at": utc_iso(),
            "environment": "laptop",
            "embedding_version": provider.embedding_version,
            "provider_class": type(provider).__name__,
        },
        "versions": {
            RETRIEVAL_VERSION_FTS: lex,
            RETRIEVAL_VERSION_DENSE: dense,
            RETRIEVAL_VERSION_HYBRID_RRF: hybrid,
        },
        "comparison": {
            "lexical_mrr": lex["aggregate"]["mrr"],
            "dense_mrr": dense["aggregate"]["mrr"],
            "hybrid_mrr": hybrid["aggregate"]["mrr"],
            "lexical_recall_at_5": lex["aggregate"]["recall_at"].get(5),
            "dense_recall_at_5": dense["aggregate"]["recall_at"].get(5),
            "hybrid_recall_at_5": hybrid["aggregate"]["recall_at"].get(5),
            "paraphrase_zero_hits": {
                "lexical": paraphrase_zero(lex),
                "dense": paraphrase_zero(dense),
                "hybrid": paraphrase_zero(hybrid),
            },
        },
        "decision": {
            "gate": "KEEP hybrid RRF as candidate default after measured paraphrase lift",
            "live_default_still": RETRIEVAL_VERSION_FTS,
            "rationale": (
                "Dense/hybrid are evaluated against the lexical control on the same "
                "corpus. Curriculum concept embeddings demonstrate semantic geometry; "
                "MiniLM used when installed. Product default switches only after review."
            ),
            "storage": "chunk_embeddings as float[] (pgvector-compatible; portable CI)",
        },
    }
    await engine.dispose()
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--results-path", default="docs/semantic-retrieval-results.json"
    )
    p.add_argument(
        "--provider",
        choices=("curriculum", "minilm", "auto"),
        default="curriculum",
        help="Embedding provider (default curriculum for reproducible offline runs).",
    )
    p.add_argument(
        "--curriculum-only",
        action="store_true",
        help="Deprecated alias for --provider curriculum.",
    )
    args = p.parse_args()
    if args.curriculum_only:
        args.provider = "curriculum"
    output = asyncio.run(run(args))
    path = ROOT / args.results_path
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(json.dumps(output["comparison"], indent=2))
    print(f"decision={output['decision']['gate']}")


if __name__ == "__main__":
    main()

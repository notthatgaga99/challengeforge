"""Hybrid vs hybrid+rerank bakeoff on the lexical eval corpus."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from challengeforge.retrieval.dense import RETRIEVAL_VERSION_HYBRID_RRF  # noqa: E402
from challengeforge.retrieval.embeddings import (  # noqa: E402
    CurriculumConceptEmbeddingProvider,
)
from challengeforge.retrieval.lexical import RETRIEVAL_VERSION_FTS  # noqa: E402
from challengeforge.retrieval.rerank import (  # noqa: E402
    RETRIEVAL_VERSION_HYBRID_RERANK,
    LexicalOverlapRerankProvider,
)
from semantic_retrieval_experiment import (  # noqa: E402
    eval_version,
    ingest_and_embed,
    load_corpus,
    setup,
)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def resolve_database_url() -> str:
    database_url = os.environ.get(
        "EXPERIMENT_DATABASE_URL",
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+asyncpg://challengeforge:challengeforge@localhost:5432/challengeforge_test",
        ),
    )
    if os.environ.get("TEST_DATABASE_URL") or os.environ.get("EXPERIMENT_DATABASE_URL"):
        return database_url
    try:
        import socket

        with socket.create_connection(("127.0.0.1", 5432), timeout=0.3):
            return database_url
    except OSError:
        pass
    from pgserver import get_server

    data = ROOT / ".pgserver-sem-exp"
    data.mkdir(exist_ok=True)
    uri = get_server(str(data)).get_uri()
    if uri.startswith("postgresql://"):
        return "postgresql+asyncpg://" + uri.removeprefix("postgresql://")
    return uri


async def run() -> dict:
    started = utc_iso()
    database_url = await resolve_database_url()
    corpus = load_corpus()
    engine, factory = await setup(database_url)
    provider = CurriculumConceptEmbeddingProvider()
    reranker = LexicalOverlapRerankProvider()
    doc_chunks = await ingest_and_embed(factory, corpus["documents"], provider)

    lex = await eval_version(factory, corpus, doc_chunks, RETRIEVAL_VERSION_FTS)
    hybrid = await eval_version(
        factory, corpus, doc_chunks, RETRIEVAL_VERSION_HYBRID_RRF, provider
    )
    reranked = await eval_version(
        factory,
        corpus,
        doc_chunks,
        RETRIEVAL_VERSION_HYBRID_RERANK,
        provider,
        rerank_provider=reranker,
    )

    h_mrr = hybrid["aggregate"]["mrr"]
    r_mrr = reranked["aggregate"]["mrr"]
    if r_mrr + 1e-9 < h_mrr:
        gate = (
            "HOLD promote rerank — curriculum overlap scorer trails hybrid MRR; "
            "live default stays cf-hybrid-rrf-1"
        )
    elif abs(r_mrr - h_mrr) < 1e-9:
        gate = (
            "KEEP cf-hybrid-rerank-1 as candidate; no MRR lift vs hybrid on this "
            "corpus — live default stays hybrid"
        )
    else:
        gate = (
            "KEEP cf-hybrid-rerank-1 as preferred ranking candidate after measured "
            "MRR lift; live default still hybrid pending product review"
        )

    results = {
        "metadata": {
            "started_at": started,
            "completed_at": utc_iso(),
            "embedding_version": provider.embedding_version,
            "rerank_version": reranker.rerank_version,
            "purpose": "hybrid vs hybrid+rerank bakeoff",
        },
        "versions": {
            RETRIEVAL_VERSION_FTS: {"aggregate": lex["aggregate"]},
            RETRIEVAL_VERSION_HYBRID_RRF: hybrid,
            RETRIEVAL_VERSION_HYBRID_RERANK: reranked,
        },
        "comparison": {
            "lexical_mrr": lex["aggregate"]["mrr"],
            "hybrid_mrr": h_mrr,
            "rerank_mrr": r_mrr,
            "hybrid_recall_at_5": hybrid["aggregate"]["recall_at"].get(5),
            "rerank_recall_at_5": reranked["aggregate"]["recall_at"].get(5),
            "mrr_delta_rerank_minus_hybrid": round(r_mrr - h_mrr, 4),
        },
        "decision": {
            "gate": gate,
            "live_default_still": "cf-hybrid-rrf-1",
            "candidate_version": RETRIEVAL_VERSION_HYBRID_RERANK,
        },
    }
    await engine.dispose()
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results-path", default="docs/rerank-bakeoff-results.json")
    args = p.parse_args()
    output = asyncio.run(run())
    path = ROOT / args.results_path
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(json.dumps(output["comparison"], indent=2))
    print(f"decision={output['decision']['gate']}")


if __name__ == "__main__":
    main()

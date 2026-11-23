"""MiniLM vs curriculum embedding bakeoff on the lexical eval corpus.

Runs one lexical control, then dense + hybrid for each embedding provider.
Curriculum proves educational semantic geometry; MiniLM is a real bi-encoder.
Does not flip the live default — that stays a product gate after review.
"""

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

from sqlalchemy import select  # noqa: E402

from challengeforge.persistence.models import DocumentChunkRow, IngestionJobRow  # noqa: E402
from challengeforge.retrieval.dense import (  # noqa: E402
    RETRIEVAL_VERSION_DENSE,
    RETRIEVAL_VERSION_HYBRID_RRF,
    index_chunks,
)
from challengeforge.retrieval.embeddings import (  # noqa: E402
    CurriculumConceptEmbeddingProvider,
    LocalMiniLMEmbeddingProvider,
)
from challengeforge.retrieval.lexical import RETRIEVAL_VERSION_FTS  # noqa: E402
from semantic_retrieval_experiment import (  # noqa: E402
    eval_version,
    ingest_and_embed,
    load_corpus,
    setup,
)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def paraphrase_zero(block: dict) -> int:
    return sum(
        1
        for q in block["queries"]
        if q["type"] == "semantic_paraphrase" and q["zero_hit"]
    )


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
    srv = get_server(str(data))
    uri = srv.get_uri()
    if uri.startswith("postgresql://"):
        return "postgresql+asyncpg://" + uri.removeprefix("postgresql://")
    return uri


async def run_arm(factory, corpus, doc_chunks, provider) -> dict:
    async with factory() as session:
        result = await session.execute(
            select(DocumentChunkRow)
            .join(
                IngestionJobRow,
                IngestionJobRow.id == DocumentChunkRow.ingestion_job_id,
            )
            .where(IngestionJobRow.status == "succeeded")
        )
        chunk_rows = list(result.scalars().all())
        n = await index_chunks(session, provider, chunk_rows)
        await session.commit()

    dense = await eval_version(
        factory, corpus, doc_chunks, RETRIEVAL_VERSION_DENSE, provider
    )
    hybrid = await eval_version(
        factory, corpus, doc_chunks, RETRIEVAL_VERSION_HYBRID_RRF, provider
    )
    return {
        "embedding_version": provider.embedding_version,
        "provider_class": type(provider).__name__,
        "indexed_embeddings": n,
        "versions": {
            RETRIEVAL_VERSION_DENSE: dense,
            RETRIEVAL_VERSION_HYBRID_RRF: hybrid,
        },
        "summary": {
            "dense_mrr": dense["aggregate"]["mrr"],
            "hybrid_mrr": hybrid["aggregate"]["mrr"],
            "dense_recall_at_5": dense["aggregate"]["recall_at"].get(5),
            "hybrid_recall_at_5": hybrid["aggregate"]["recall_at"].get(5),
            "paraphrase_zero_hits": {
                "dense": paraphrase_zero(dense),
                "hybrid": paraphrase_zero(hybrid),
            },
        },
    }


async def run() -> dict:
    started = utc_iso()
    database_url = await resolve_database_url()
    corpus = load_corpus()
    engine, factory = await setup(database_url)

    curriculum = CurriculumConceptEmbeddingProvider()
    doc_chunks = await ingest_and_embed(factory, corpus["documents"], curriculum)
    lex = await eval_version(factory, corpus, doc_chunks, RETRIEVAL_VERSION_FTS)
    curriculum_arm = await run_arm(factory, corpus, doc_chunks, curriculum)

    try:
        minilm = LocalMiniLMEmbeddingProvider()
    except RuntimeError as exc:
        await engine.dispose()
        raise SystemExit(
            f"MiniLM unavailable ({exc}). Install: pip install '.[embeddings]'"
        ) from exc

    minilm_arm = await run_arm(factory, corpus, doc_chunks, minilm)

    lex_summary = {
        "mrr": lex["aggregate"]["mrr"],
        "recall_at_5": lex["aggregate"]["recall_at"].get(5),
        "paraphrase_zero_hits": paraphrase_zero(lex),
    }

    c_h = curriculum_arm["summary"]["hybrid_mrr"]
    m_h = minilm_arm["summary"]["hybrid_mrr"]
    m_para = minilm_arm["summary"]["paraphrase_zero_hits"]["hybrid"]
    if m_para > lex_summary["paraphrase_zero_hits"]:
        gate = "HOLD promote MiniLM — hybrid paraphrase worse than lexical control"
    elif m_h + 1e-9 < c_h:
        # Curriculum is hand-tuned to this corpus; a small MRR gap is expected.
        gate = (
            "KEEP MiniLM as preferred real encoder (paraphrase closed); "
            "curriculum remains offline/CI demo; live default still lexical "
            "(MiniLM hybrid MRR trails curriculum on this small corpus)"
        )
    else:
        gate = (
            "KEEP MiniLM as preferred real encoder for candidate hybrid; "
            "live default still lexical pending product review"
        )

    results = {
        "metadata": {
            "started_at": started,
            "completed_at": utc_iso(),
            "environment": "laptop",
            "corpus": "docs/lexical-retrieval-corpus.json",
            "purpose": "curriculum vs MiniLM bakeoff on same judged queries",
        },
        "lexical_control": {
            "retrieval_version": RETRIEVAL_VERSION_FTS,
            **lex_summary,
            "full": lex,
        },
        "arms": {
            "curriculum": curriculum_arm,
            "minilm": minilm_arm,
        },
        "comparison": {
            "lexical": lex_summary,
            "curriculum": curriculum_arm["summary"],
            "minilm": minilm_arm["summary"],
            "hybrid_mrr_delta_minilm_minus_curriculum": round(m_h - c_h, 4),
            "dense_mrr_delta_minilm_minus_curriculum": round(
                minilm_arm["summary"]["dense_mrr"]
                - curriculum_arm["summary"]["dense_mrr"],
                4,
            ),
        },
        "decision": {
            "gate": gate,
            "live_default_still": RETRIEVAL_VERSION_FTS,
            "candidate_hybrid": RETRIEVAL_VERSION_HYBRID_RRF,
            "preferred_real_encoder": minilm.embedding_version,
            "curriculum_role": (
                "offline/CI semantic geometry demo (not production quality claim)"
            ),
            "rationale": (
                "Same corpus and metrics as semantic retrieval milestone. "
                "MiniLM is a real bi-encoder; curriculum is educational. "
                "Product search default remains lexical until an explicit promote milestone."
            ),
        },
    }
    await engine.dispose()
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--results-path",
        default="docs/minilm-bakeoff-results.json",
    )
    args = p.parse_args()
    output = asyncio.run(run())
    path = ROOT / args.results_path
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(json.dumps(output["comparison"], indent=2))
    print(f"decision={output['decision']['gate']}")


if __name__ == "__main__":
    main()

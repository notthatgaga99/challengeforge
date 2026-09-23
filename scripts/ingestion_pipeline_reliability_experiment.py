"""Ingestion pipeline reliability / capacity experiment.

Uses embedded or TEST database via Settings.database_url override through env
when provided; otherwise creates an isolated schema with Base.metadata.

Decision target: PostgreSQL-backed durable jobs (no Redis/Kafka).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from challengeforge.config import Settings  # noqa: E402
from challengeforge.domain.enums import (  # noqa: E402
    ChallengeStatus,
    HackathonStatus,
    SubmissionStatus,
)
from challengeforge.identity import ORGANIZER_ID, PARTICIPANT_ID  # noqa: E402
from challengeforge.ingestion.processor import result_key_for  # noqa: E402
from challengeforge.ingestion.worker import IngestionWorker  # noqa: E402
from challengeforge.persistence.mapping import utcnow  # noqa: E402
from challengeforge.persistence.models import (  # noqa: E402
    Base,
    ChallengeRow,
    HackathonRow,
    IngestionJobRow,
    SubmissionRow,
)
from challengeforge.persistence.repositories import IngestionJobRepository  # noqa: E402
from challengeforge.persistence.seed import seed_dev_users  # noqa: E402
from challengeforge.storage.filesystem import LocalFilesystemStorage  # noqa: E402


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def setup(database_url: str, root: Path):
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = LocalFilesystemStorage(root / "artifacts")
    settings = Settings(
        artifact_root=root / "artifacts",
        ingestion_max_attempts=3,
        ingestion_stale_after_seconds=30,
        ingestion_retry_delay_seconds=0.0,
        ingestion_poll_interval_seconds=0.05,
    )
    return engine, factory, storage, settings


async def enqueue_n(factory, storage, n: int, body: bytes = b"doc"):
    async with factory() as session:
        await seed_dev_users(session)
        now = utcnow()
        hack = HackathonRow(
            id=uuid4(),
            title="H",
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
        for i in range(n):
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
            key = f"submissions/{chal.id}/{PARTICIPANT_ID}/{i}.bin"
            storage.put(key, body if body != b"doc" else f"doc {i}".encode(), "text/plain")
            await IngestionJobRepository(session).enqueue(
                submission_id=sub.id, artifact_key=key
            )
        await session.commit()


async def drain(workers: list[IngestionWorker], rounds: int = 200) -> dict:
    t0 = time.perf_counter()

    async def one(w: IngestionWorker) -> None:
        for _ in range(rounds):
            if not await w.run_once():
                await asyncio.sleep(0.01)

    await asyncio.gather(*(one(w) for w in workers))
    return {
        "wall_s": round(time.perf_counter() - t0, 4),
        "succeeded": sum(w.succeeded for w in workers),
        "failed": sum(w.failed for w in workers),
        "retried": sum(w.retried for w in workers),
        "claims": sum(w.claims for w in workers),
    }


async def backdate_running(factory) -> None:
    async with factory() as session:
        rows = (await session.execute(select(IngestionJobRow))).scalars().all()
        for row in rows:
            if row.started_at is not None:
                row.started_at = utcnow() - timedelta(seconds=60)
        await session.commit()


async def run(args) -> dict:
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

            data = ROOT / ".pgserver-ingestion-exp"
            data.mkdir(exist_ok=True)
            srv = get_server(str(data))
            database_url = srv.get_uri()
            if database_url.startswith("postgresql://"):
                database_url = "postgresql+asyncpg://" + database_url.removeprefix(
                    "postgresql://"
                )

    root = ROOT / ".cf_gov_ws" / "ingestion_exp" / str(uuid4())
    root.mkdir(parents=True)
    engine, factory, storage, settings = await setup(database_url, root)

    results: dict = {"metadata": {"started_at": utc_iso(), "database": "local"}}

    capacity = []
    for workers_n, jobs in ((1, 20), (2, 50), (4, 100)):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await enqueue_n(factory, storage, jobs)
        ws = [
            IngestionWorker(
                worker_id=f"c{i}",
                session_factory=factory,
                storage=storage,
                settings=settings,
            )
            for i in range(workers_n)
        ]
        stats = await drain(ws, rounds=jobs + 20)
        async with factory() as session:
            counts = await IngestionJobRepository(session).count_by_status()
        capacity.append(
            {
                "workers": workers_n,
                "jobs": jobs,
                **stats,
                "counts": counts,
                "jobs_per_s": round(jobs / stats["wall_s"], 2) if stats["wall_s"] else None,
                "ok": counts.get("succeeded") == jobs,
            }
        )
        print(f"capacity workers={workers_n} jobs={jobs} {capacity[-1]}", flush=True)
    results["capacity"] = capacity

    failures: dict = {}

    async def reset_world():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    settings_fast = Settings(
        artifact_root=root / "artifacts",
        ingestion_max_attempts=3,
        ingestion_stale_after_seconds=0,
        ingestion_retry_delay_seconds=0.0,
        ingestion_poll_interval_seconds=0.05,
    )

    await reset_world()
    await enqueue_n(factory, storage, 1)
    async with factory() as session:
        counts = await IngestionJobRepository(session).count_by_status()
    failures["job_never_started"] = {
        "counts": counts,
        "ok": counts.get("queued") == 1 and sum(counts.values()) == 1,
    }
    print(f"failure job_never_started {failures['job_never_started']}", flush=True)

    await reset_world()
    await enqueue_n(factory, storage, 1, body=b"claim-die")
    w = IngestionWorker(
        worker_id="f-claim",
        session_factory=factory,
        storage=storage,
        settings=settings_fast,
    )
    w.crash_after_claim = True
    try:
        await w.run_once()
        claim_die_raised = False
    except RuntimeError:
        claim_die_raised = True
    async with factory() as session:
        mid = await IngestionJobRepository(session).count_by_status()
    await backdate_running(factory)
    await w.recover_stale()
    w2 = IngestionWorker(
        worker_id="f-claim-2",
        session_factory=factory,
        storage=storage,
        settings=settings_fast,
    )
    await w2.run_once()
    async with factory() as session:
        after = await IngestionJobRepository(session).count_by_status()
    failures["claim_then_die"] = {
        "raised": claim_die_raised,
        "after_crash": mid,
        "after_recover": after,
        "ok": claim_die_raised
        and mid.get("running") == 1
        and after.get("succeeded") == 1,
    }
    print(f"failure claim_then_die {failures['claim_then_die']}", flush=True)

    await reset_world()
    await enqueue_n(factory, storage, 1, body=b"ack-die content")
    w = IngestionWorker(
        worker_id="f-ack",
        session_factory=factory,
        storage=storage,
        settings=settings_fast,
    )
    w.crash_after_process = True
    try:
        await w.run_once()
        ack_raised = False
    except RuntimeError:
        ack_raised = True
    async with factory() as session:
        mid = await IngestionJobRepository(session).count_by_status()
        rows = (await session.execute(select(IngestionJobRow))).scalars().all()
        jid = rows[0].id
    blob_before = storage.exists(result_key_for(jid))
    await backdate_running(factory)
    await w.recover_stale()
    w2 = IngestionWorker(
        worker_id="f-ack-2",
        session_factory=factory,
        storage=storage,
        settings=settings_fast,
    )
    await w2.run_once()
    async with factory() as session:
        after = await IngestionJobRepository(session).count_by_status()
        job = await IngestionJobRepository(session).get(jid)
    failures["die_after_process_before_ack"] = {
        "raised": ack_raised,
        "blob_after_crash": blob_before,
        "after_crash_counts": mid,
        "final_status": job.status.value if job else None,
        "final_result_exists": bool(
            job and job.result_key and storage.exists(job.result_key)
        ),
        "ok": ack_raised
        and blob_before
        and mid.get("running") == 1
        and after.get("succeeded") == 1
        and job is not None
        and job.result_key is not None
        and storage.exists(job.result_key),
    }
    print(
        f"failure die_after_process_before_ack {failures['die_after_process_before_ack']}",
        flush=True,
    )

    await reset_world()
    async with factory() as session:
        await seed_dev_users(session)
        now = utcnow()
        hack = HackathonRow(
            id=uuid4(),
            title="H",
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
        for i, body in enumerate(
            [b"cf_ingest_poison x"] + [f"ok {i}".encode() for i in range(10)]
        ):
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
            key = f"submissions/{chal.id}/{PARTICIPANT_ID}/p{i}.bin"
            storage.put(key, body, "text/plain")
            await IngestionJobRepository(session).enqueue(
                submission_id=sub.id, artifact_key=key
            )
        await session.commit()
    w = IngestionWorker(
        worker_id="poison", session_factory=factory, storage=storage, settings=settings
    )
    poison_stats = await drain([w], rounds=40)
    async with factory() as session:
        counts = await IngestionJobRepository(session).count_by_status()
    failures["poison_isolation"] = {
        **poison_stats,
        "counts": counts,
        "ok": counts.get("succeeded") == 10 and counts.get("failed") == 1,
    }
    print(f"failure poison_isolation {failures['poison_isolation']}", flush=True)

    await reset_world()
    await enqueue_n(factory, storage, 1, body=b"cf_ingest_transient x")
    w = IngestionWorker(
        worker_id="f-tr",
        session_factory=factory,
        storage=storage,
        settings=settings_fast,
    )
    await w.run_once()
    async with factory() as session:
        mid = await IngestionJobRepository(session).count_by_status()
    await w.run_once()
    async with factory() as session:
        after = await IngestionJobRepository(session).count_by_status()
    failures["transient_then_success"] = {
        "after_first": mid,
        "after_second": after,
        "retried": w.retried,
        "succeeded": w.succeeded,
        "ok": mid.get("queued") == 1 and after.get("succeeded") == 1 and w.retried == 1,
    }
    print(
        f"failure transient_then_success {failures['transient_then_success']}",
        flush=True,
    )

    results["failure_injection"] = failures
    results["poison_isolation"] = failures["poison_isolation"]

    results["decision"] = {
        "gate": "KEEP PostgreSQL-backed durable ingestion jobs",
        "rationale": (
            "At-least-once claim with SKIP LOCKED, idempotent result_key overwrite, "
            "deterministic poison terminal, stale RUNNING recovery, and measured "
            "laptop throughput do not justify Redis/Kafka/Celery yet."
        ),
        "exactly_once_execution": False,
        "exactly_once_effects": (
            "approximated via idempotent result overwrite + conditional complete"
        ),
        "external_queue": False,
        "failure_cells_ok": all(v.get("ok") for v in failures.values()),
    }
    results["metadata"]["completed_at"] = utc_iso()
    await engine.dispose()
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--results-path", default="docs/ingestion-pipeline-reliability-results.json"
    )
    args = p.parse_args()
    output = asyncio.run(run(args))
    path = ROOT / args.results_path
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(f"decision={output['decision']['gate']}")
    if not output["decision"]["failure_cells_ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

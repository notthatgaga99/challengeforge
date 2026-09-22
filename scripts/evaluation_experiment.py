#!/usr/bin/env python
"""Adversarial evaluation-pipeline experiments (A–F).

Uses the same Experiment helpers as the concurrency harness. Does not modify
historical concurrency baseline or correctness result files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import event, text, update

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from challengeforge.config import Settings
from challengeforge.main import create_app
from challengeforge.persistence import session as session_module
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.models import EvaluationRow
from challengeforge.persistence.session import create_engine as create_cf_engine
from challengeforge.worker import EvaluationWorker
from concurrency_experiment import (
    PARTICIPANT_ID,
    Experiment,
    PoolTracker,
    UvicornThread,
    asyncpg_url,
    choose_port,
    percentile,
    utc_iso,
    wait_for_server,
)
from sqlalchemy.ext.asyncio import async_sessionmaker

RESULTS_PATH = ROOT / "docs" / "evaluation-pipeline-results.json"


def latency(values: list[float]) -> dict[str, float]:
    return {
        "min_ms": round(min(values), 2) if values else 0.0,
        "mean_ms": round(sum(values) / len(values), 2) if values else 0.0,
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "max_ms": round(max(values), 2) if values else 0.0,
    }


class EvaluationExperiment(Experiment):
    def __init__(self, database_url: str, base_url: str) -> None:
        super().__init__(database_url, base_url)
        self.worker_engine = create_cf_engine(
            Settings(database_url=database_url, db_pool_size=5, db_max_overflow=5)
        )
        self.worker_sessions = async_sessionmaker(
            self.worker_engine, expire_on_commit=False
        )

    async def close(self) -> None:
        await self.worker_engine.dispose()
        await super().close()

    def make_worker(self, settings: Settings, worker_id: str) -> EvaluationWorker:
        return EvaluationWorker(
            settings, worker_id=worker_id, session_factory=self.worker_sessions
        )

    async def create_submitted(
        self,
        client: httpx.AsyncClient,
        challenge_id: str,
        *,
        metadata: dict[str, Any] | None = None,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        hdrs = dict(headers or {"X-User-Id": str(PARTICIPANT_ID)})
        if key:
            hdrs["Idempotency-Key"] = key
        created = await self.request(
            client,
            "POST",
            f"/api/v1/challenges/{challenge_id}/submissions",
            headers=hdrs,
            json={"metadata": metadata or {"note": "evaluation-experiment"}},
        )
        assert created.status in (200, 201), created
        submission_id = created.body["id"]
        submitted = await self.request(
            client,
            "POST",
            f"/api/v1/submissions/{submission_id}/submit",
            headers={"X-User-Id": hdrs["X-User-Id"]},
        )
        assert submitted.status == 200, submitted
        return submitted.body

    async def get_evaluation(
        self, client: httpx.AsyncClient, submission_id: str
    ) -> dict[str, Any]:
        obs = await self.request(
            client,
            "GET",
            f"/api/v1/submissions/{submission_id}/evaluation",
            headers={"X-User-Id": str(PARTICIPANT_ID)},
        )
        assert obs.status == 200, obs
        return obs.body

    async def experiment_a_normal(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(client, "Eval A")
        body = await self.create_submitted(client, challenge_id, key="eval-a")
        assert body["evaluation_status"] == "queued"
        assert body["evaluation_async"] is True

        settings = Settings(
            database_url=self.database_url,
            artifact_root=ROOT / "data" / "evaluation-artifacts",
            evaluation_fake_work_ms=10,
            evaluation_poll_interval_seconds=0.05,
            log_level="WARNING",
        )
        worker = self.make_worker(settings, "exp-a")
        await worker.drain(timeout_seconds=30)

        evaluation = await self.get_evaluation(client, body["id"])
        return {
            "passed": evaluation["status"] == "succeeded" and evaluation["score"] is not None,
            "submission_id": body["id"],
            "evaluation": evaluation,
            "worker_claims": worker.claims,
            "worker_succeeded": worker.jobs_completed,
        }

    async def experiment_b_two_workers(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(client, "Eval B")
        body = await self.create_submitted(client, challenge_id, key="eval-b")
        settings = Settings(
            database_url=self.database_url,
            artifact_root=ROOT / "data" / "evaluation-artifacts",
            evaluation_fake_work_ms=30,
            evaluation_poll_interval_seconds=0.01,
            log_level="WARNING",
        )
        w1 = self.make_worker(settings, "exp-b-1")
        w2 = self.make_worker(settings, "exp-b-2")
        await asyncio.gather(
            w1.drain(max_idle_rounds=4, timeout_seconds=30),
            w2.drain(max_idle_rounds=4, timeout_seconds=30),
        )
        evaluation = await self.get_evaluation(client, body["id"])
        total_claims = w1.claims + w2.claims
        total_success = w1.jobs_completed + w2.jobs_completed
        return {
            "passed": (
                total_claims == 1
                and total_success == 1
                and evaluation["status"] == "succeeded"
                and evaluation["attempt_count"] == 1
            ),
            "claims": total_claims,
            "succeeded": total_success,
            "evaluation": evaluation,
            "worker_1_claims": w1.claims,
            "worker_2_claims": w2.claims,
        }

    async def experiment_c_many(
        self, client: httpx.AsyncClient, count: int = 100, workers: int = 4
    ) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(client, "Eval C")
        submission_ids: list[str] = []
        for i in range(count):
            body = await self.create_submitted(
                client,
                challenge_id,
                key=f"eval-c-{i}",
                metadata={"index": i},
                headers=self.participant_headers[i % len(self.participant_headers)],
            )
            submission_ids.append(body["id"])

        settings = Settings(
            database_url=self.database_url,
            artifact_root=ROOT / "data" / "evaluation-artifacts",
            evaluation_fake_work_ms=15,
            evaluation_poll_interval_seconds=0.01,
            log_level="WARNING",
        )
        worker_objs = [self.make_worker(settings, f"exp-c-{i}") for i in range(workers)]
        started = time.perf_counter()
        await asyncio.gather(
            *[w.drain(max_idle_rounds=5, timeout_seconds=120) for w in worker_objs]
        )
        elapsed = time.perf_counter() - started

        queue_waits: list[float] = []
        executions: list[float] = []
        e2e: list[float] = []
        succeeded = 0
        async with self.worker_sessions() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT e.status, e.created_at, e.started_at, e.completed_at
                        FROM evaluations e
                        JOIN submissions s ON s.id = e.submission_id
                        WHERE s.challenge_id = :challenge_id
                        ORDER BY e.created_at
                        """
                    ),
                    {"challenge_id": challenge_id},
                )
            ).mappings().all()
            assert len(rows) == count, f"expected {count} evaluations, got {len(rows)}"
            for row in rows:
                if row["status"] == "succeeded":
                    succeeded += 1
                if row["started_at"] and row["created_at"]:
                    queue_waits.append(
                        (row["started_at"] - row["created_at"]).total_seconds() * 1000
                    )
                if row["completed_at"] and row["started_at"]:
                    executions.append(
                        (row["completed_at"] - row["started_at"]).total_seconds() * 1000
                    )
                if row["completed_at"] and row["created_at"]:
                    e2e.append(
                        (row["completed_at"] - row["created_at"]).total_seconds() * 1000
                    )

        claims = sum(w.claims for w in worker_objs)
        return {
            "passed": succeeded == count and claims == count,
            "queued": count,
            "succeeded": succeeded,
            "claims": claims,
            "workers": workers,
            "wall_seconds": round(elapsed, 3),
            "throughput_evaluations_per_second": round(
                succeeded / elapsed if elapsed else 0.0, 2
            ),
            "queue_wait_ms": latency(queue_waits),
            "execution_ms": latency(executions),
            "end_to_end_ms": latency(e2e),
            "per_worker_claims": [w.claims for w in worker_objs],
            "per_worker_succeeded": [w.jobs_completed for w in worker_objs],
        }

    async def experiment_d_failure(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(client, "Eval D")
        body = await self.create_submitted(
            client,
            challenge_id,
            key="eval-d",
            metadata={"force_evaluation_failure": True},
        )
        settings = Settings(
            database_url=self.database_url,
            artifact_root=ROOT / "data" / "evaluation-artifacts",
            evaluation_fake_work_ms=5,
            log_level="WARNING",
        )
        worker = self.make_worker(settings, "exp-d")
        await worker.drain(timeout_seconds=30)
        evaluation = await self.get_evaluation(client, body["id"])
        return {
            "passed": (
                evaluation["status"] == "failed"
                and evaluation["score"] is None
                and "Forced" in (evaluation["failure_reason"] or "")
            ),
            "evaluation": evaluation,
            "jobs_failed": worker.jobs_failed,
        }

    async def experiment_e_crash(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(client, "Eval E")
        body = await self.create_submitted(client, challenge_id, key="eval-e")
        evaluation_id = UUID(body["evaluation_id"])

        async with self.worker_sessions() as session:
            await session.execute(
                update(EvaluationRow)
                .where(EvaluationRow.id == evaluation_id)
                .values(
                    status="running",
                    started_at=utcnow() - timedelta(seconds=120),
                    attempt_count=1,
                    worker_id="crashed",
                )
            )
            await session.commit()

        settings = Settings(
            database_url=self.database_url,
            artifact_root=ROOT / "data" / "evaluation-artifacts",
            evaluation_stale_after_seconds=30,
            evaluation_max_attempts=3,
            evaluation_fake_work_ms=10,
            log_level="WARNING",
        )
        worker = self.make_worker(settings, "exp-e-recovery")
        recovered = await worker.recover_stale()
        await worker.drain(timeout_seconds=30)
        evaluation = await self.get_evaluation(client, body["id"])
        return {
            "passed": (
                recovered == 1
                and evaluation["status"] == "succeeded"
                and evaluation["attempt_count"] == 2
            ),
            "recovered": recovered,
            "evaluation": evaluation,
        }

    async def experiment_f_idempotency(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(client, "Eval F")
        key = f"eval-f-{uuid4()}"
        gate = asyncio.Event()

        async def create_once() -> Any:
            await gate.wait()
            return await self.request(
                client,
                "POST",
                f"/api/v1/challenges/{challenge_id}/submissions",
                headers={
                    "X-User-Id": str(PARTICIPANT_ID),
                    "Idempotency-Key": key,
                },
                json={"metadata": {"same": True}},
            )

        tasks = [asyncio.create_task(create_once()) for _ in range(20)]
        gate.set()
        observations = await asyncio.gather(*tasks)
        statuses = [o.status for o in observations]
        ids = {o.body["id"] for o in observations if o.body}
        assert len(ids) == 1
        submission_id = next(iter(ids))

        submit_gate = asyncio.Event()

        async def submit_once() -> Any:
            await submit_gate.wait()
            return await self.request(
                client,
                "POST",
                f"/api/v1/submissions/{submission_id}/submit",
                headers={"X-User-Id": str(PARTICIPANT_ID)},
            )

        submit_tasks = [asyncio.create_task(submit_once()) for _ in range(10)]
        submit_gate.set()
        submit_obs = await asyncio.gather(*submit_tasks)
        eval_ids = {
            o.body.get("evaluation_id")
            for o in submit_obs
            if o.status == 200 and o.body
        }

        async with self.worker_sessions() as session:
            this_count = (
                await session.execute(
                    text(
                        "SELECT count(*)::int FROM evaluations WHERE submission_id = :sid"
                    ),
                    {"sid": submission_id},
                )
            ).scalar_one()

        return {
            "passed": len(ids) == 1 and this_count == 1 and len(eval_ids) == 1,
            "create_status_counts": {
                str(s): statuses.count(s) for s in set(statuses)
            },
            "submission_ids": list(ids),
            "evaluation_ids": list(eval_ids),
            "evaluations_for_submission": this_count,
            "submit_statuses": [o.status for o in submit_obs],
        }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    embedded = None
    if args.database_url:
        database_url = args.database_url
        database_mode = "explicit"
    else:
        from pgserver import get_server

        data_dir = ROOT / ".pgserver-evaluation"
        data_dir.mkdir(exist_ok=True)
        embedded = get_server(str(data_dir))
        database_url = asyncpg_url(embedded.get_uri())
        database_mode = "embedded PostgreSQL"

    port = choose_port()
    base_url = f"http://127.0.0.1:{port}"
    experiment = EvaluationExperiment(database_url, base_url)
    await experiment.prepare_database()
    settings = Settings(
        database_url=database_url,
        artifact_root=ROOT / "data" / "evaluation-artifacts",
        log_level="WARNING",
        db_pool_size=5,
        db_max_overflow=5,
        evaluation_fake_work_ms=15,
    )
    server = UvicornThread(create_app(settings), "127.0.0.1", port)
    server.start()
    await wait_for_server(base_url)

    assert session_module._engine is not None
    experiment.pool_tracker = PoolTracker()
    event.listen(
        session_module._engine.sync_engine.pool,
        "checkout",
        experiment.pool_tracker.checkout,
    )
    event.listen(
        session_module._engine.sync_engine.pool,
        "checkin",
        experiment.pool_tracker.checkin,
    )

    results: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "database_mode": database_mode,
            "database_version": None,
            "transaction_isolation": None,
        },
        "scenarios": {},
    }
    try:
        async with experiment.db.connect() as connection:
            results["metadata"]["database_version"] = (
                await connection.execute(text("SHOW server_version"))
            ).scalar_one()
            results["metadata"]["transaction_isolation"] = (
                await connection.execute(text("SHOW transaction_isolation"))
            ).scalar_one()

        limits = httpx.Limits(max_connections=100, max_keepalive_connections=40)
        async with httpx.AsyncClient(
            base_url=base_url, timeout=60, limits=limits
        ) as client:
            scenarios = [
                ("A_normal_evaluation", experiment.experiment_a_normal),
                ("B_two_workers", experiment.experiment_b_two_workers),
                ("C_many_evaluations", experiment.experiment_c_many),
                ("D_evaluator_failure", experiment.experiment_d_failure),
                ("E_worker_crash", experiment.experiment_e_crash),
                ("F_submission_idempotency", experiment.experiment_f_idempotency),
            ]
            for name, op in scenarios:
                print(f"running {name}", flush=True)
                result, resources = await experiment.monitored(lambda op=op: op(client))
                results["scenarios"][name] = {
                    "result": result,
                    "resources": resources,
                }
                print(
                    f"  passed={result.get('passed')} resources_peak_rss_mb="
                    f"{resources.get('process', {}).get('peak_rss_mb')}",
                    flush=True,
                )
    finally:
        results["metadata"]["completed_at"] = utc_iso()
        server.stop()
        await experiment.close()
        _ = embedded

    all_passed = all(
        scenario["result"].get("passed") for scenario in results["scenarios"].values()
    )
    results["metadata"]["all_passed"] = all_passed
    RESULTS_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run ChallengeForge evaluation pipeline adversarial experiments."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("EXPERIMENT_DATABASE_URL", ""),
        help="Destructive isolated PostgreSQL URL; defaults to embedded PostgreSQL.",
    )
    args = parser.parse_args()
    results = asyncio.run(run(args))
    print(f"wrote {RESULTS_PATH}")
    print(f"all_passed={results['metadata']['all_passed']}")
    for name, scenario in results["scenarios"].items():
        print(f"  {name}: passed={scenario['result'].get('passed')}")


if __name__ == "__main__":
    main()

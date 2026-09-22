#!/usr/bin/env python
"""Repeatable PostgreSQL concurrency experiment for ChallengeForge.

This is deliberately an observation harness, not a correctness fix. It starts
the current FastAPI application with its configured 5+5 SQLAlchemy pool against
an isolated embedded PostgreSQL instance by default, drives concurrent HTTP
requests, samples PostgreSQL/pool/process state, and writes machine-readable
results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import socket
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Awaitable, Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import psutil
import uvicorn
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from challengeforge.config import Settings
from challengeforge.domain.enums import UserRole
from challengeforge.identity import ORGANIZER_ID, PARTICIPANT_ID
from challengeforge.main import create_app
from challengeforge.persistence.models import Base, SubmissionRow, UserRow

ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = ROOT / "docs" / "concurrency-results.json"
PARTICIPANT_COUNT = 100


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 2)


def latency_summary(values: list[float]) -> dict[str, float]:
    return {
        "min_ms": round(min(values), 2) if values else 0.0,
        "mean_ms": round(mean(values), 2) if values else 0.0,
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "max_ms": round(max(values), 2) if values else 0.0,
    }


@dataclass
class RequestObservation:
    status: int | None
    latency_ms: float
    body: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class ResourceSample:
    timestamp: float
    db_connections: int
    db_active: int
    db_lock_waiters: int
    oldest_transaction_ms: float
    pool_checked_out: int
    pool_size: int
    pool_overflow: int
    app_cpu_percent: float
    app_rss_mb: float
    db_cpu_percent: float
    db_rss_mb: float


@dataclass
class MonitorResult:
    samples: list[ResourceSample] = field(default_factory=list)
    deadlocks_delta: int = 0
    conflicts_delta: int = 0

    def summary(self) -> dict[str, Any]:
        if not self.samples:
            return {"samples": 0}
        return {
            "samples": len(self.samples),
            "max_db_connections": max(s.db_connections for s in self.samples),
            "max_db_active": max(s.db_active for s in self.samples),
            "max_db_lock_waiters": max(s.db_lock_waiters for s in self.samples),
            "max_transaction_ms": round(
                max(s.oldest_transaction_ms for s in self.samples), 2
            ),
            "max_pool_checked_out": max(s.pool_checked_out for s in self.samples),
            "max_pool_size": max(s.pool_size for s in self.samples),
            "max_pool_overflow": max(s.pool_overflow for s in self.samples),
            "max_app_cpu_percent": round(
                max(s.app_cpu_percent for s in self.samples), 2
            ),
            "max_app_rss_mb": round(max(s.app_rss_mb for s in self.samples), 2),
            "max_db_cpu_percent": round(
                max(s.db_cpu_percent for s in self.samples), 2
            ),
            "max_db_rss_mb": round(max(s.db_rss_mb for s in self.samples), 2),
            "deadlocks_delta": self.deadlocks_delta,
            "conflicts_delta": self.conflicts_delta,
        }


class PoolTracker:
    """Exact SQLAlchemy checkout tracking; sampling can miss short checkouts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0
        self.total_checkouts = 0

    def checkout(self, *_: Any) -> None:
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
            self.total_checkouts += 1

    def checkin(self, *_: Any) -> None:
        with self._lock:
            self.current -= 1

    def reset(self) -> None:
        with self._lock:
            self.peak = self.current
            self.total_checkouts = 0

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "exact_peak_pool_checked_out": self.peak,
                "pool_checkouts": self.total_checkouts,
            }


class ExperimentMonitor:
    def __init__(self, database_url: str, interval: float = 0.02) -> None:
        self.engine = create_async_engine(
            database_url, poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        self.interval = interval
        self.stop_event = asyncio.Event()
        self.result = MonitorResult()
        self.process = psutil.Process()
        self.postgres_processes = self._postgres_processes()
        self._baseline: tuple[int, int] = (0, 0)

    def _postgres_processes(self) -> list[psutil.Process]:
        processes: list[psutil.Process] = []
        for process in psutil.process_iter(["name"]):
            try:
                if "postgres" in (process.info["name"] or "").lower():
                    processes.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return processes

    async def _database_counters(self) -> tuple[int, int]:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT deadlocks, conflicts
                        FROM pg_stat_database
                        WHERE datname = current_database()
                        """
                    )
                )
            ).one()
            return int(row.deadlocks), int(row.conflicts)

    async def start(self) -> None:
        self._baseline = await self._database_counters()
        self.process.cpu_percent(None)
        for process in self.postgres_processes:
            try:
                process.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    async def run(self) -> None:
        from challengeforge.persistence import session as session_module

        await self.start()
        async with self.engine.connect() as connection:
            while not self.stop_event.is_set():
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT
                                count(*)::int AS connections,
                                count(*) FILTER (WHERE state = 'active')::int AS active,
                                count(*) FILTER (
                                    WHERE wait_event_type = 'Lock'
                                )::int AS lock_waiters,
                                COALESCE(
                                    max(EXTRACT(EPOCH FROM (clock_timestamp() - xact_start))
                                        * 1000) FILTER (WHERE xact_start IS NOT NULL),
                                    0
                                )::float AS oldest_transaction_ms
                            FROM pg_stat_activity
                            WHERE datname = current_database()
                            """
                        )
                    )
                ).one()
                engine = session_module._engine
                pool = engine.pool if engine is not None else None
                app_memory = self.process.memory_info().rss / 1024 / 1024
                db_cpu = 0.0
                db_memory = 0.0
                for process in self.postgres_processes:
                    try:
                        db_cpu += process.cpu_percent(None)
                        db_memory += process.memory_info().rss / 1024 / 1024
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                self.result.samples.append(
                    ResourceSample(
                        timestamp=time.perf_counter(),
                        db_connections=int(row.connections),
                        db_active=int(row.active),
                        db_lock_waiters=int(row.lock_waiters),
                        oldest_transaction_ms=float(row.oldest_transaction_ms),
                        pool_checked_out=pool.checkedout() if pool else 0,
                        pool_size=pool.size() if pool else 0,
                        pool_overflow=pool.overflow() if pool else 0,
                        app_cpu_percent=self.process.cpu_percent(None),
                        app_rss_mb=app_memory,
                        db_cpu_percent=db_cpu,
                        db_rss_mb=db_memory,
                    )
                )
                await asyncio.sleep(self.interval)
        final = await self._database_counters()
        self.result.deadlocks_delta = final[0] - self._baseline[0]
        self.result.conflicts_delta = final[1] - self._baseline[1]

    def stop(self) -> None:
        self.stop_event.set()

    async def close(self) -> None:
        await self.engine.dispose()


class UvicornThread:
    def __init__(self, app: Any, host: str, port: int) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class Experiment:
    def __init__(self, database_url: str, base_url: str) -> None:
        self.database_url = database_url
        self.base_url = base_url
        self.db = create_async_engine(
            database_url, pool_size=2, max_overflow=0, pool_pre_ping=True
        )
        self.organizer_headers = {"X-User-Id": str(ORGANIZER_ID)}
        self.participant_ids = [
            uuid5(NAMESPACE_URL, f"challengeforge-concurrency-{index}")
            for index in range(PARTICIPANT_COUNT)
        ]
        self.participant_headers = [
            {"X-User-Id": str(participant_id)}
            for participant_id in self.participant_ids
        ]
        self.pool_tracker: PoolTracker | None = None

    async def prepare_database(self) -> None:
        async with self.db.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with self.db.begin() as connection:
            await connection.execute(
                UserRow.__table__.insert(),
                [
                    {
                        "id": ORGANIZER_ID,
                        "display_name": "Concurrency Organizer",
                        "role": UserRole.ORGANIZER.value,
                        "created_at": datetime.now(timezone.utc),
                    },
                    {
                        "id": PARTICIPANT_ID,
                        "display_name": "Primary Participant",
                        "role": UserRole.PARTICIPANT.value,
                        "created_at": datetime.now(timezone.utc),
                    },
                    *[
                        {
                            "id": participant_id,
                            "display_name": f"Participant {index:03d}",
                            "role": UserRole.PARTICIPANT.value,
                            "created_at": datetime.now(timezone.utc),
                        }
                        for index, participant_id in enumerate(self.participant_ids)
                    ],
                ],
            )

    async def create_published_challenge(
        self, client: httpx.AsyncClient, title: str
    ) -> tuple[str, str]:
        hackathon = (
            await client.post(
                "/api/v1/hackathons",
                headers=self.organizer_headers,
                json={"title": f"{title} Hackathon", "description": "Concurrency run"},
            )
        ).json()
        await client.post(
            f"/api/v1/hackathons/{hackathon['id']}/publish",
            headers=self.organizer_headers,
        )
        challenge = (
            await client.post(
                f"/api/v1/hackathons/{hackathon['id']}/challenges",
                headers=self.organizer_headers,
                json={
                    "title": title,
                    "description": "Concurrency target",
                    "constraints": "",
                    "specification": {"body": "Submit a JSON metadata object."},
                    "evaluation_criteria": [
                        {
                            "name": "Exists",
                            "description": "A valid submission exists.",
                            "weight": 1,
                        }
                    ],
                },
            )
        ).json()
        await client.post(
            f"/api/v1/challenges/{challenge['id']}/publish",
            headers=self.organizer_headers,
        )
        return hackathon["id"], challenge["id"]

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> RequestObservation:
        started = time.perf_counter()
        try:
            response = await client.request(method, path, **kwargs)
            latency = (time.perf_counter() - started) * 1000
            try:
                body = response.json()
            except ValueError:
                body = None
            return RequestObservation(response.status_code, latency, body)
        except Exception as exc:
            return RequestObservation(
                None, (time.perf_counter() - started) * 1000, error=repr(exc)
            )

    async def monitored(
        self, operation: Callable[[], Awaitable[dict[str, Any]]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.pool_tracker is not None:
            self.pool_tracker.reset()
        monitor = ExperimentMonitor(self.database_url)
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.05)
        try:
            result = await operation()
        finally:
            monitor.stop()
            await task
            await monitor.close()
        resources = monitor.result.summary()
        if self.pool_tracker is not None:
            resources.update(self.pool_tracker.snapshot())
        return result, resources

    async def count_submissions(self, challenge_id: str) -> int:
        async with self.db.connect() as connection:
            return int(
                (
                    await connection.execute(
                        select(func.count())
                        .select_from(SubmissionRow)
                        .where(SubmissionRow.challenge_id == UUID(challenge_id))
                    )
                ).scalar_one()
            )

    async def submission_diagnostics(self, challenge_id: str) -> dict[str, int]:
        async with self.db.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            count(*)::int AS rows,
                            count(DISTINCT participant_id)::int AS participants,
                            count(DISTINCT id)::int AS distinct_ids,
                            (
                                count(*) - count(DISTINCT created_at)
                            )::int AS duplicate_timestamps
                        FROM submissions
                        WHERE challenge_id = :challenge_id
                        """
                    ),
                    {"challenge_id": UUID(challenge_id)},
                )
            ).one()
            return {
                "rows": int(row.rows),
                "participants": int(row.participants),
                "distinct_ids": int(row.distinct_ids),
                "duplicate_timestamps": int(row.duplicate_timestamps),
            }

    async def scenario_a(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(
            client, "A duplicate request"
        )
        key = f"scenario-a-{uuid4()}"
        gate = asyncio.Event()

        async def send() -> RequestObservation:
            await gate.wait()
            return await self.request(
                client,
                "POST",
                f"/api/v1/challenges/{challenge_id}/submissions",
                headers={
                    "X-User-Id": str(PARTICIPANT_ID),
                    "Idempotency-Key": key,
                },
                json={"metadata": {"logical_submission": "same"}},
            )

        async def operation() -> dict[str, Any]:
            tasks = [asyncio.create_task(send()) for _ in range(20)]
            gate.set()
            observations = await asyncio.gather(*tasks)
            ids = [o.body.get("id") for o in observations if o.body and o.body.get("id")]
            return {
                "requests": len(observations),
                "status_counts": dict(Counter(o.status for o in observations)),
                "errors": [o.error for o in observations if o.error],
                "latency": latency_summary([o.latency_ms for o in observations]),
                "distinct_response_ids": len(set(ids)),
                "database_rows": await self.count_submissions(challenge_id),
                "replayed_responses": sum(
                    1
                    for o in observations
                    if o.body and o.body.get("idempotent_replay") is True
                ),
            }

        result, resources = await self.monitored(operation)
        return {"result": result, "resources": resources}

    async def scenario_b(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(
            client, "B independent submissions"
        )
        gate = asyncio.Event()

        async def send(index: int) -> RequestObservation:
            await gate.wait()
            return await self.request(
                client,
                "POST",
                f"/api/v1/challenges/{challenge_id}/submissions",
                headers={
                    **self.participant_headers[index],
                    "Idempotency-Key": f"scenario-b-{index}",
                },
                json={"metadata": {"participant_number": index}},
            )

        async def operation() -> dict[str, Any]:
            tasks = [
                asyncio.create_task(send(index)) for index in range(PARTICIPANT_COUNT)
            ]
            gate.set()
            observations = await asyncio.gather(*tasks)
            ids = [o.body.get("id") for o in observations if o.body and o.body.get("id")]
            diagnostics = await self.submission_diagnostics(challenge_id)
            return {
                "requests": len(observations),
                "status_counts": dict(Counter(o.status for o in observations)),
                "successes": sum(1 for o in observations if o.status == 201),
                "failures": sum(1 for o in observations if o.status != 201),
                "client_errors": [o.error for o in observations if o.error],
                "latency": latency_summary([o.latency_ms for o in observations]),
                "distinct_response_ids": len(set(ids)),
                "database": diagnostics,
                "pool_timeout_errors": sum(
                    1
                    for o in observations
                    if o.error and "timeout" in o.error.lower()
                ),
            }

        result, resources = await self.monitored(operation)
        return {"result": result, "resources": resources}

    async def scenario_c(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(
            client, "C read write contention"
        )
        stop_readers = asyncio.Event()
        read_observations: dict[str, list[RequestObservation]] = {
            "challenge": [],
            "challenge_submissions": [],
            "participant_submissions": [],
        }
        observed_counts: list[int] = []

        async def read_challenge() -> None:
            while not stop_readers.is_set():
                read_observations["challenge"].append(
                    await self.request(
                        client,
                        "GET",
                        f"/api/v1/challenges/{challenge_id}",
                        headers=self.participant_headers[0],
                    )
                )
                await asyncio.sleep(0)

        async def read_challenge_submissions() -> None:
            while not stop_readers.is_set():
                observation = await self.request(
                    client,
                    "GET",
                    f"/api/v1/challenges/{challenge_id}/submissions",
                    headers=self.organizer_headers,
                )
                read_observations["challenge_submissions"].append(observation)
                if isinstance(observation.body, list):
                    observed_counts.append(len(observation.body))
                await asyncio.sleep(0)

        async def read_participant_submissions() -> None:
            participant_id = self.participant_ids[0]
            while not stop_readers.is_set():
                read_observations["participant_submissions"].append(
                    await self.request(
                        client,
                        "GET",
                        f"/api/v1/users/{participant_id}/submissions",
                        headers=self.participant_headers[0],
                    )
                )
                await asyncio.sleep(0)

        async def operation() -> dict[str, Any]:
            readers = [
                asyncio.create_task(read_challenge()),
                asyncio.create_task(read_challenge_submissions()),
                asyncio.create_task(read_participant_submissions()),
            ]
            await asyncio.sleep(0.1)
            writes = await asyncio.gather(
                *[
                    self.request(
                        client,
                        "POST",
                        f"/api/v1/challenges/{challenge_id}/submissions",
                        headers={
                            **self.participant_headers[index],
                            "Idempotency-Key": f"scenario-c-{index}",
                        },
                        json={"metadata": {"contention": index}},
                    )
                    for index in range(60)
                ]
            )
            await asyncio.sleep(0.1)
            stop_readers.set()
            await asyncio.gather(*readers)
            final_rows = await self.count_submissions(challenge_id)
            flattened_reads = [
                item for observations in read_observations.values() for item in observations
            ]
            non_monotonic = sum(
                1
                for previous, current in zip(observed_counts, observed_counts[1:])
                if current < previous
            )
            return {
                "writes": {
                    "requests": len(writes),
                    "status_counts": dict(Counter(o.status for o in writes)),
                    "latency": latency_summary([o.latency_ms for o in writes]),
                    "failures": sum(1 for o in writes if o.status != 201),
                },
                "reads": {
                    name: {
                        "requests": len(observations),
                        "status_counts": dict(Counter(o.status for o in observations)),
                        "latency": latency_summary(
                            [o.latency_ms for o in observations]
                        ),
                    }
                    for name, observations in read_observations.items()
                },
                "total_read_failures": sum(
                    1 for o in flattened_reads if o.status != 200
                ),
                "observed_submission_counts": observed_counts,
                "non_monotonic_submission_counts": non_monotonic,
                "final_database_rows": final_rows,
                "leaderboard": "not implemented",
            }

        result, resources = await self.monitored(operation)
        return {"result": result, "resources": resources}

    async def scenario_d(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(
            client, "D challenge close race"
        )
        async with self.db.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE OR REPLACE FUNCTION cf_experiment_delay_submission()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        PERFORM pg_sleep(0.75);
                        RETURN NEW;
                    END
                    $$
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TRIGGER cf_experiment_delay_submission_trigger
                    BEFORE INSERT ON submissions
                    FOR EACH ROW EXECUTE FUNCTION cf_experiment_delay_submission()
                    """
                )
            )

        async def wait_until_insert_is_sleeping() -> bool:
            deadline = time.perf_counter() + 3
            while time.perf_counter() < deadline:
                async with self.db.connect() as connection:
                    found = (
                        await connection.execute(
                            text(
                                """
                                SELECT EXISTS(
                                    SELECT 1
                                    FROM pg_stat_activity
                                    WHERE datname = current_database()
                                      AND query ILIKE 'INSERT INTO submissions%'
                                      AND wait_event = 'PgSleep'
                                )
                                """
                            )
                        )
                    ).scalar_one()
                if found:
                    return True
                await asyncio.sleep(0.01)
            return False

        async def operation() -> dict[str, Any]:
            request_task = asyncio.create_task(
                self.request(
                    client,
                    "POST",
                    f"/api/v1/challenges/{challenge_id}/submissions",
                    headers={
                        **self.participant_headers[0],
                        "Idempotency-Key": "scenario-d-in-flight",
                    },
                    json={"metadata": {"race": "in-flight-before-close"}},
                )
            )
            insert_was_sleeping = await wait_until_insert_is_sleeping()
            close_started = time.perf_counter()
            async with self.db.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE challenges
                        SET status = 'closed', updated_at = clock_timestamp()
                        WHERE id = :challenge_id
                        """
                    ),
                    {"challenge_id": UUID(challenge_id)},
                )
            close_committed_at = time.perf_counter()
            in_flight = await request_task
            request_completed_at = time.perf_counter()
            after_close = await self.request(
                client,
                "POST",
                f"/api/v1/challenges/{challenge_id}/submissions",
                headers={
                    **self.participant_headers[1],
                    "Idempotency-Key": "scenario-d-after-close",
                },
                json={"metadata": {"race": "started-after-close"}},
            )
            return {
                "insert_observed_sleeping_before_close": insert_was_sleeping,
                "close_commit_duration_ms": round(
                    (close_committed_at - close_started) * 1000, 2
                ),
                "in_flight_status": in_flight.status,
                "in_flight_completed_after_close_ms": round(
                    (request_completed_at - close_committed_at) * 1000, 2
                ),
                "request_started_after_close_status": after_close.status,
                "database_rows": await self.count_submissions(challenge_id),
                "close_mechanism": (
                    "direct SQL because the current API has no close endpoint; "
                    "the database status value is part of the current model"
                ),
            }

        try:
            result, resources = await self.monitored(operation)
        finally:
            async with self.db.begin() as connection:
                await connection.execute(
                    text(
                        "DROP TRIGGER IF EXISTS "
                        "cf_experiment_delay_submission_trigger ON submissions"
                    )
                )
                await connection.execute(
                    text(
                        "DROP FUNCTION IF EXISTS cf_experiment_delay_submission()"
                    )
                )
        return {"result": result, "resources": resources}

    async def scenario_e(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(
            client, "E lost response retry"
        )
        key = f"scenario-e-{uuid4()}"

        async def operation() -> dict[str, Any]:
            first = await self.request(
                client,
                "POST",
                f"/api/v1/challenges/{challenge_id}/submissions",
                headers={
                    **self.participant_headers[0],
                    "Idempotency-Key": key,
                },
                json={"metadata": {"response": "discarded"}},
            )
            retry = await self.request(
                client,
                "POST",
                f"/api/v1/challenges/{challenge_id}/submissions",
                headers={
                    **self.participant_headers[0],
                    "Idempotency-Key": key,
                },
                json={"metadata": {"response": "discarded"}},
            )
            with_key_rows = await self.count_submissions(challenge_id)
            no_key_first = await self.request(
                client,
                "POST",
                f"/api/v1/challenges/{challenge_id}/submissions",
                headers=self.participant_headers[1],
                json={"metadata": {"no_key": True}},
            )
            no_key_retry = await self.request(
                client,
                "POST",
                f"/api/v1/challenges/{challenge_id}/submissions",
                headers=self.participant_headers[1],
                json={"metadata": {"no_key": True}},
            )
            final_rows = await self.count_submissions(challenge_id)
            return {
                "with_key": {
                    "first_status": first.status,
                    "retry_status": retry.status,
                    "same_response_id": bool(
                        first.body
                        and retry.body
                        and first.body.get("id") == retry.body.get("id")
                    ),
                    "rows_after_retry": with_key_rows,
                },
                "without_key": {
                    "first_status": no_key_first.status,
                    "retry_status": no_key_retry.status,
                    "same_response_id": bool(
                        no_key_first.body
                        and no_key_retry.body
                        and no_key_first.body.get("id")
                        == no_key_retry.body.get("id")
                    ),
                    "additional_rows": final_rows - with_key_rows,
                },
                "final_database_rows": final_rows,
            }

        result, resources = await self.monitored(operation)
        return {"result": result, "resources": resources}

    async def scenario_f(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(
            client, "F terminal transition race"
        )
        created = await self.request(
            client,
            "POST",
            f"/api/v1/challenges/{challenge_id}/submissions",
            headers={
                **self.participant_headers[0],
                "Idempotency-Key": "scenario-f",
            },
            json={"metadata": {"transition": "race"}},
        )
        assert created.body is not None
        submission_id = created.body["id"]
        gate = asyncio.Event()

        async def transition(action: str) -> RequestObservation:
            await gate.wait()
            return await self.request(
                client,
                "POST",
                f"/api/v1/submissions/{submission_id}/{action}",
                headers=self.participant_headers[0],
            )

        async def operation() -> dict[str, Any]:
            submit_task = asyncio.create_task(transition("submit"))
            cancel_task = asyncio.create_task(transition("cancel"))
            gate.set()
            submitted, cancelled = await asyncio.gather(submit_task, cancel_task)
            final = await self.request(
                client,
                "GET",
                f"/api/v1/submissions/{submission_id}",
                headers=self.participant_headers[0],
            )
            return {
                "submit_response": {
                    "status": submitted.status,
                    "reported_state": (
                        submitted.body.get("status") if submitted.body else None
                    ),
                },
                "cancel_response": {
                    "status": cancelled.status,
                    "reported_state": (
                        cancelled.body.get("status") if cancelled.body else None
                    ),
                },
                "final_database_state": (
                    final.body.get("status") if final.body else None
                ),
                "both_terminal_transitions_accepted": (
                    submitted.status == 200 and cancelled.status == 200
                ),
            }

        result, resources = await self.monitored(operation)
        return {"result": result, "resources": resources}

    async def close(self) -> None:
        await self.db.dispose()


def choose_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def asyncpg_url(uri: str) -> str:
    if uri.startswith("postgresql+asyncpg://"):
        return uri
    return "postgresql+asyncpg://" + uri.removeprefix("postgresql://")


async def wait_for_server(base_url: str) -> None:
    deadline = time.perf_counter() + 10
    async with httpx.AsyncClient(base_url=base_url) as client:
        while time.perf_counter() < deadline:
            try:
                if (await client.get("/health")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise RuntimeError("ChallengeForge server did not become ready.")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    embedded = None
    if args.database_url:
        database_url = args.database_url
        database_mode = "explicit"
    else:
        from pgserver import get_server

        data_dir = ROOT / ".pgserver-concurrency"
        data_dir.mkdir(exist_ok=True)
        embedded = get_server(str(data_dir))
        database_url = asyncpg_url(embedded.get_uri())
        database_mode = "embedded PostgreSQL"

    port = choose_port()
    base_url = f"http://127.0.0.1:{port}"
    experiment = Experiment(database_url, base_url)
    await experiment.prepare_database()

    settings = Settings(
        database_url=database_url,
        artifact_root=ROOT / "data" / "concurrency-artifacts",
        log_level="WARNING",
        db_pool_size=5,
        db_max_overflow=5,
    )
    server = UvicornThread(create_app(settings), "127.0.0.1", port)
    server.start()
    await wait_for_server(base_url)
    from challengeforge.persistence import session as session_module

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
            "app_pool_size": settings.db_pool_size,
            "app_max_overflow": settings.db_max_overflow,
            "participant_rows": PARTICIPANT_COUNT + 2,
            "leaderboard_implemented": False,
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

        limits = httpx.Limits(
            max_connections=220, max_keepalive_connections=100
        )
        async with httpx.AsyncClient(
            base_url=base_url, timeout=45, limits=limits
        ) as client:
            scenarios: list[
                tuple[str, Callable[[httpx.AsyncClient], Awaitable[dict[str, Any]]]]
            ] = [
                ("A_concurrent_duplicate_request", experiment.scenario_a),
                ("B_concurrent_independent_submissions", experiment.scenario_b),
                ("C_read_write_contention", experiment.scenario_c),
                ("D_challenge_closing_race", experiment.scenario_d),
                ("E_lost_response_retry", experiment.scenario_e),
                ("F_terminal_transition_race", experiment.scenario_f),
            ]
            for name, scenario in scenarios:
                print(f"running {name}", flush=True)
                results["scenarios"][name] = await scenario(client)
                print(
                    json.dumps(results["scenarios"][name]["result"], indent=2),
                    flush=True,
                )
    finally:
        results["metadata"]["completed_at"] = utc_iso()
        server.stop()
        await experiment.close()
        # Keep a reference until all DB work and the app server have stopped.
        _ = embedded

    RESULTS_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run ChallengeForge's repeatable PostgreSQL concurrency experiment."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("EXPERIMENT_DATABASE_URL", ""),
        help=(
            "Destructive isolated database URL. When omitted, use embedded PostgreSQL. "
            "The selected database schema is dropped and recreated."
        ),
    )
    args = parser.parse_args()
    results = asyncio.run(run(args))
    print(f"wrote {RESULTS_PATH}")
    print(
        json.dumps(
            {
                name: scenario["result"]
                for name, scenario in results["scenarios"].items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

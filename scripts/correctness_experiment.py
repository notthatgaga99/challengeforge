#!/usr/bin/env python
"""Adversarial verification and cost measurement for concurrency design v2.

This imports only reusable setup/monitoring helpers from the historical
experiment. It never overwrites the baseline results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import event, text

from challengeforge.config import Settings
from challengeforge.main import create_app
from challengeforge.persistence import session as session_module

from cf_experiment_paths import ensure_experiment_paths

ensure_experiment_paths()

from concurrency_experiment import (
    PARTICIPANT_ID,
    ROOT,
    Experiment,
    PoolTracker,
    RequestObservation,
    UvicornThread,
    asyncpg_url,
    choose_port,
    percentile,
    utc_iso,
    wait_for_server,
)

RESULTS_PATH = ROOT / "docs" / "concurrency-correctness-results.json"
BASELINE_PATH = ROOT / "docs" / "concurrency-results.json"


def latency(values: list[float]) -> dict[str, float]:
    return {
        "min_ms": round(min(values), 2) if values else 0.0,
        "mean_ms": round(sum(values) / len(values), 2) if values else 0.0,
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": round(max(values), 2) if values else 0.0,
    }


def request_metrics(
    observations: list[RequestObservation], duration_seconds: float
) -> dict[str, Any]:
    return {
        "requests": len(observations),
        "status_counts": dict(Counter(item.status for item in observations)),
        "latency": latency([item.latency_ms for item in observations]),
        "throughput_rps": round(
            len(observations) / duration_seconds if duration_seconds else 0.0, 2
        ),
        "errors": [item.error for item in observations if item.error],
    }


class CorrectnessExperiment(Experiment):
    async def wait_for_lock_waiters(self, expected: int, timeout: float = 5) -> None:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            async with self.db.connect() as connection:
                count = int(
                    (
                        await connection.execute(
                            text(
                                """
                                SELECT count(*)::int
                                FROM pg_stat_activity
                                WHERE datname = current_database()
                                  AND wait_event_type = 'Lock'
                                """
                            )
                        )
                    ).scalar_one()
                )
            if count >= expected:
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"Expected {expected} PostgreSQL lock waiters.")

    async def performance_duplicate(
        self, client: httpx.AsyncClient, request_count: int = 50
    ) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(
            client, "V2 duplicate performance"
        )
        gate = asyncio.Event()
        key = f"v2-duplicate-{uuid4()}"

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
                json={"metadata": {"logical": "same"}},
            )

        async def operation() -> dict[str, Any]:
            tasks = [asyncio.create_task(send()) for _ in range(request_count)]
            started = time.perf_counter()
            gate.set()
            observations = await asyncio.gather(*tasks)
            elapsed = time.perf_counter() - started
            ids = {
                item.body["id"]
                for item in observations
                if item.body and "id" in item.body
            }
            return {
                **request_metrics(observations, elapsed),
                "distinct_response_ids": len(ids),
                "database_rows": await self.count_submissions(challenge_id),
                "replayed": sum(
                    1
                    for item in observations
                    if item.body and item.body.get("idempotent_replay") is True
                ),
            }

        result, resources = await self.monitored(operation)
        return {"result": result, "resources": resources}

    async def performance_independent(
        self, client: httpx.AsyncClient, request_count: int = 100
    ) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(
            client, "V2 independent performance"
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
                    "Idempotency-Key": f"v2-independent-{index}",
                },
                json={"metadata": {"participant": index}},
            )

        async def operation() -> dict[str, Any]:
            tasks = [asyncio.create_task(send(index)) for index in range(request_count)]
            started = time.perf_counter()
            gate.set()
            observations = await asyncio.gather(*tasks)
            elapsed = time.perf_counter() - started
            diagnostics = await self.submission_diagnostics(challenge_id)
            return {
                **request_metrics(observations, elapsed),
                "database": diagnostics,
                "pool_timeout_errors": sum(
                    1
                    for item in observations
                    if item.error and "timeout" in item.error.lower()
                ),
            }

        result, resources = await self.monitored(operation)
        return {"result": result, "resources": resources}

    async def terminal_races(
        self, client: httpx.AsyncClient, iterations_each: int = 20
    ) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(
            client, "V2 terminal races"
        )
        patterns = [("submit", "cancel"), ("cancel", "submit")]
        outcomes: dict[str, Any] = {}
        all_observations: list[RequestObservation] = []
        total_started = time.perf_counter()

        for winner_action, loser_action in patterns:
            successes = 0
            conflicts = 0
            final_matches = 0
            for iteration in range(iterations_each):
                created = await self.request(
                    client,
                    "POST",
                    f"/api/v1/challenges/{challenge_id}/submissions",
                    headers={
                        **self.participant_headers[iteration],
                        "Idempotency-Key": (
                            f"v2-transition-{winner_action}-{iteration}"
                        ),
                    },
                    json={"metadata": {"pattern": winner_action}},
                )
                assert created.body is not None
                submission_id = created.body["id"]

                async with self.db.connect() as blocker:
                    transaction = await blocker.begin()
                    await blocker.execute(
                        text(
                            """
                            SELECT id FROM submissions
                            WHERE id = :submission_id
                            FOR UPDATE
                            """
                        ),
                        {"submission_id": UUID(submission_id)},
                    )
                    winner_task = asyncio.create_task(
                        self.request(
                            client,
                            "POST",
                            f"/api/v1/submissions/{submission_id}/{winner_action}",
                            headers=self.participant_headers[iteration],
                        )
                    )
                    await self.wait_for_lock_waiters(1)
                    loser_task = asyncio.create_task(
                        self.request(
                            client,
                            "POST",
                            f"/api/v1/submissions/{submission_id}/{loser_action}",
                            headers=self.participant_headers[iteration],
                        )
                    )
                    await self.wait_for_lock_waiters(2)
                    await transaction.commit()
                    winner_response, loser_response = await asyncio.gather(
                        winner_task, loser_task
                    )

                all_observations.extend([winner_response, loser_response])
                successes += int(winner_response.status == 200)
                conflicts += int(loser_response.status == 409)
                final = await self.request(
                    client,
                    "GET",
                    f"/api/v1/submissions/{submission_id}",
                    headers=self.participant_headers[iteration],
                )
                expected = "submitted" if winner_action == "submit" else "cancelled"
                final_matches += int(
                    final.body is not None and final.body.get("status") == expected
                )

            outcomes[f"{winner_action}_wins"] = {
                "iterations": iterations_each,
                "winner_successes": successes,
                "loser_conflicts": conflicts,
                "final_state_matches_winner": final_matches,
            }

        elapsed = time.perf_counter() - total_started
        outcomes["request_metrics"] = request_metrics(all_observations, elapsed)
        return outcomes

    async def close_acceptance_races(
        self, client: httpx.AsyncClient, iterations_each: int = 10
    ) -> dict[str, Any]:
        outcomes: dict[str, Any] = {}
        all_observations: list[RequestObservation] = []
        started = time.perf_counter()

        for first in ("submission", "close"):
            accepted = 0
            rejected = 0
            closes = 0
            for iteration in range(iterations_each):
                _, challenge_id = await self.create_published_challenge(
                    client, f"V2 close {first} {iteration}"
                )
                participant_headers = {
                    **self.participant_headers[iteration],
                    "Idempotency-Key": f"v2-close-{first}-{iteration}",
                }
                async with self.db.connect() as blocker:
                    transaction = await blocker.begin()
                    await blocker.execute(
                        text(
                            """
                            SELECT id FROM challenges
                            WHERE id = :challenge_id
                            FOR UPDATE
                            """
                        ),
                        {"challenge_id": UUID(challenge_id)},
                    )

                    submission_request = lambda: self.request(
                        client,
                        "POST",
                        f"/api/v1/challenges/{challenge_id}/submissions",
                        headers=participant_headers,
                        json={"metadata": {"first": first}},
                    )
                    close_request = lambda: self.request(
                        client,
                        "POST",
                        f"/api/v1/challenges/{challenge_id}/close",
                        headers=self.organizer_headers,
                    )

                    first_task = asyncio.create_task(
                        submission_request() if first == "submission" else close_request()
                    )
                    await self.wait_for_lock_waiters(1)
                    second_task = asyncio.create_task(
                        close_request() if first == "submission" else submission_request()
                    )
                    await self.wait_for_lock_waiters(2)
                    await transaction.commit()
                    first_response, second_response = await asyncio.gather(
                        first_task, second_task
                    )

                submission_response = (
                    first_response if first == "submission" else second_response
                )
                close_response = second_response if first == "submission" else first_response
                all_observations.extend([submission_response, close_response])
                accepted += int(submission_response.status == 201)
                rejected += int(submission_response.status == 409)
                closes += int(close_response.status == 200)

            outcomes[f"{first}_first"] = {
                "iterations": iterations_each,
                "submission_accepted": accepted,
                "submission_rejected": rejected,
                "close_successes": closes,
            }

        elapsed = time.perf_counter() - started
        outcomes["request_metrics"] = request_metrics(all_observations, elapsed)
        return outcomes

    async def conflicting_idempotency(
        self, client: httpx.AsyncClient, iterations: int = 20
    ) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(
            client, "V2 fingerprint races"
        )
        all_observations: list[RequestObservation] = []
        correct_pairs = 0
        started = time.perf_counter()

        for iteration in range(iterations):
            gate = asyncio.Event()
            headers = {
                **self.participant_headers[iteration],
                "Idempotency-Key": f"v2-fingerprint-{iteration}",
            }

            async def send(revision: int) -> RequestObservation:
                await gate.wait()
                return await self.request(
                    client,
                    "POST",
                    f"/api/v1/challenges/{challenge_id}/submissions",
                    headers=headers,
                    json={"metadata": {"revision": revision}},
                )

            first = asyncio.create_task(send(1))
            second = asyncio.create_task(send(2))
            gate.set()
            responses = list(await asyncio.gather(first, second))
            all_observations.extend(responses)
            correct_pairs += int(
                sorted(item.status for item in responses if item.status is not None)
                == [201, 409]
            )

        elapsed = time.perf_counter() - started
        return {
            "iterations": iterations,
            "one_create_one_conflict": correct_pairs,
            "database_rows": await self.count_submissions(challenge_id),
            "request_metrics": request_metrics(all_observations, elapsed),
        }

    async def mixed_workload(self, client: httpx.AsyncClient) -> dict[str, Any]:
        _, challenge_id = await self.create_published_challenge(
            client, "V2 mixed workload"
        )
        initial: list[dict[str, Any]] = []
        for index in range(20):
            response = await self.request(
                client,
                "POST",
                f"/api/v1/challenges/{challenge_id}/submissions",
                headers={
                    **self.participant_headers[index],
                    "Idempotency-Key": f"v2-mixed-initial-{index}",
                },
                json={"metadata": {"initial": index}},
            )
            assert response.body is not None
            initial.append(response.body)

        stop_reads = asyncio.Event()
        reads: list[RequestObservation] = []

        async def reader() -> None:
            while not stop_reads.is_set():
                reads.append(
                    await self.request(
                        client,
                        "GET",
                        f"/api/v1/challenges/{challenge_id}",
                        headers=self.participant_headers[0],
                    )
                )
                await asyncio.sleep(0)

        read_task = asyncio.create_task(reader())
        transition_tasks = []
        for index, submission in enumerate(initial):
            for action in ("submit", "cancel"):
                transition_tasks.append(
                    asyncio.create_task(
                        self.request(
                            client,
                            "POST",
                            f"/api/v1/submissions/{submission['id']}/{action}",
                            headers=self.participant_headers[index],
                        )
                    )
                )

        create_tasks = [
            asyncio.create_task(
                self.request(
                    client,
                    "POST",
                    f"/api/v1/challenges/{challenge_id}/submissions",
                    headers={
                        **self.participant_headers[index + 20],
                        "Idempotency-Key": f"v2-mixed-new-{index}",
                    },
                    json={"metadata": {"new": index}},
                )
            )
            for index in range(40)
        ]
        await asyncio.sleep(0.01)
        close = await self.request(
            client,
            "POST",
            f"/api/v1/challenges/{challenge_id}/close",
            headers=self.organizer_headers,
        )
        transitions = await asyncio.gather(*transition_tasks)
        creates = await asyncio.gather(*create_tasks)

        post_close = [
            await self.request(
                client,
                "POST",
                f"/api/v1/challenges/{challenge_id}/submissions",
                headers={
                    **self.participant_headers[index + 70],
                    "Idempotency-Key": f"v2-post-close-{index}",
                },
                json={"metadata": {"post_close": index}},
            )
            for index in range(10)
        ]
        stop_reads.set()
        await read_task

        async with self.db.connect() as connection:
            integrity = (
                await connection.execute(
                    text(
                        """
                        SELECT
                          count(*) FILTER (
                            WHERE u.id IS NULL OR c.id IS NULL
                          )::int AS broken_foreign_keys,
                          count(*) FILTER (
                            WHERE s.status NOT IN (
                              'created', 'submitted', 'cancelled', 'failed'
                            )
                          )::int AS illegal_statuses
                        FROM submissions s
                        LEFT JOIN users u ON u.id = s.participant_id
                        LEFT JOIN challenges c ON c.id = s.challenge_id
                        WHERE s.challenge_id = :challenge_id
                        """
                    ),
                    {"challenge_id": UUID(challenge_id)},
                )
            ).one()
            duplicate_keys = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*)::int
                            FROM (
                              SELECT participant_id, idempotency_key
                              FROM submissions
                              WHERE challenge_id = :challenge_id
                                AND idempotency_key IS NOT NULL
                              GROUP BY participant_id, idempotency_key
                              HAVING count(*) > 1
                            ) duplicates
                            """
                        ),
                        {"challenge_id": UUID(challenge_id)},
                    )
                ).scalar_one()
            )

        terminal_statuses = Counter(item.status for item in transitions)
        return {
            "close_status": close.status,
            "transition_status_counts": dict(terminal_statuses),
            "exactly_one_terminal_success_per_submission": (
                terminal_statuses[200] == 20 and terminal_statuses[409] == 20
            ),
            "create_status_counts": dict(Counter(item.status for item in creates)),
            "post_close_status_counts": dict(
                Counter(item.status for item in post_close)
            ),
            "post_close_successes": sum(
                1 for item in post_close if item.status == 201
            ),
            "read_status_counts": dict(Counter(item.status for item in reads)),
            "broken_foreign_keys": int(integrity.broken_foreign_keys),
            "illegal_statuses": int(integrity.illegal_statuses),
            "duplicate_key_groups": duplicate_keys,
            "final_database_rows": await self.count_submissions(challenge_id),
        }


def baseline_comparison(
    baseline: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    pairs = {
        "duplicate_requests": (
            baseline["scenarios"]["A_concurrent_duplicate_request"],
            after["duplicate_requests"],
        ),
        "independent_writes": (
            baseline["scenarios"]["B_concurrent_independent_submissions"],
            after["independent_writes"],
        ),
    }
    comparison: dict[str, Any] = {}
    for name, (before_scenario, after_scenario) in pairs.items():
        before = before_scenario["result"]["latency"]
        current = after_scenario["result"]["latency"]
        comparison[name] = {
            "before": {
                "request_count": before_scenario["result"]["requests"],
                "p50_ms": before["p50_ms"],
                "p95_ms": before["p95_ms"],
                "p99_ms": "not captured in baseline",
            },
            "after": {
                "request_count": after_scenario["result"]["requests"],
                "p50_ms": current["p50_ms"],
                "p95_ms": current["p95_ms"],
                "p99_ms": current["p99_ms"],
                "throughput_rps": after_scenario["result"]["throughput_rps"],
            },
            "note": (
                "Single laptop runs are noisy; duplicate request counts differ "
                "(baseline 20, after 50). Independent writes are both 100."
            ),
        }
    return comparison


async def run(args: argparse.Namespace) -> dict[str, Any]:
    embedded = None
    if args.database_url:
        database_url = args.database_url
        database_mode = "explicit"
    else:
        from pgserver import get_server

        data_dir = ROOT / ".pgserver-correctness"
        data_dir.mkdir(exist_ok=True)
        embedded = get_server(str(data_dir))
        database_url = asyncpg_url(embedded.get_uri())
        database_mode = "embedded PostgreSQL"

    port = choose_port()
    base_url = f"http://127.0.0.1:{port}"
    experiment = CorrectnessExperiment(database_url, base_url)
    await experiment.prepare_database()
    settings = Settings(
        database_url=database_url,
        artifact_root=ROOT / "data" / "correctness-artifacts",
        log_level="WARNING",
        db_pool_size=5,
        db_max_overflow=5,
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

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    results: dict[str, Any] = {
        "metadata": {
            "started_at": utc_iso(),
            "database_mode": database_mode,
            "database_version": None,
            "transaction_isolation": None,
            "app_pool_size": 5,
            "app_max_overflow": 5,
            "baseline_file": str(BASELINE_PATH.relative_to(ROOT)),
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

        limits = httpx.Limits(max_connections=220, max_keepalive_connections=100)
        async with httpx.AsyncClient(
            base_url=base_url, timeout=45, limits=limits
        ) as client:
            measured = [
                ("duplicate_requests", experiment.performance_duplicate),
                ("independent_writes", experiment.performance_independent),
            ]
            for name, operation in measured:
                print(f"running {name}", flush=True)
                results["scenarios"][name] = await operation(client)

            print("running terminal_races", flush=True)
            terminal_result, terminal_resources = await experiment.monitored(
                lambda: experiment.terminal_races(client)
            )
            results["scenarios"]["terminal_races"] = {
                "result": terminal_result,
                "resources": terminal_resources,
            }

            print("running close_acceptance_races", flush=True)
            close_result, close_resources = await experiment.monitored(
                lambda: experiment.close_acceptance_races(client)
            )
            results["scenarios"]["close_acceptance_races"] = {
                "result": close_result,
                "resources": close_resources,
            }

            print("running conflicting_idempotency", flush=True)
            conflict_result, conflict_resources = await experiment.monitored(
                lambda: experiment.conflicting_idempotency(client)
            )
            results["scenarios"]["conflicting_idempotency"] = {
                "result": conflict_result,
                "resources": conflict_resources,
            }

            print("running mixed_workload", flush=True)
            mixed_result, mixed_resources = await experiment.monitored(
                lambda: experiment.mixed_workload(client)
            )
            results["scenarios"]["mixed_workload"] = {
                "result": mixed_result,
                "resources": mixed_resources,
            }

        results["before_after"] = baseline_comparison(baseline, results["scenarios"])
    finally:
        results["metadata"]["completed_at"] = utc_iso()
        server.stop()
        await experiment.close()
        _ = embedded

    RESULTS_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify ChallengeForge concurrency correctness mechanisms."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("EXPERIMENT_DATABASE_URL", ""),
        help="Destructive isolated PostgreSQL URL; defaults to embedded PostgreSQL.",
    )
    args = parser.parse_args()
    results = asyncio.run(run(args))
    print(f"wrote {RESULTS_PATH}")
    print(json.dumps(results["before_after"], indent=2))


if __name__ == "__main__":
    main()

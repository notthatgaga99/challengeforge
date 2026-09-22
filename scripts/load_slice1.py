#!/usr/bin/env python
"""Load-test hook for ChallengeForge slice 1.

This is not a pass/fail gate. It exists so the next iteration can stress:

- challenge browsing
- challenge retrieval
- submission creation
- submission status retrieval

Examples:

    python scripts/load_slice1.py --rate 20 --duration 30
    python scripts/load_slice1.py --rate 50 --duration 10 --burst

Assumption: one published challenge is enough to represent the hot path.
Reconsider the script when we add evaluation or AI endpoints that dominate latency.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from dataclasses import dataclass

import httpx

ORGANIZER_ID = "11111111-1111-4111-8111-111111111111"
PARTICIPANT_ID = "22222222-2222-4222-8222-222222222222"


@dataclass
class Stats:
    ok: int = 0
    err: int = 0
    latencies: list[float] = None

    def __post_init__(self) -> None:
        if self.latencies is None:
            self.latencies = []


async def ensure_challenge(client: httpx.AsyncClient) -> str:
    headers = {"X-User-Id": ORGANIZER_ID}
    hackathon = (
        await client.post(
            "/api/v1/hackathons",
            headers=headers,
            json={"title": "Load Event", "description": "Created by load_slice1.py"},
        )
    ).json()
    await client.post(f"/api/v1/hackathons/{hackathon['id']}/publish", headers=headers)
    challenge = (
        await client.post(
            f"/api/v1/hackathons/{hackathon['id']}/challenges",
            headers=headers,
            json={
                "title": "Load challenge",
                "description": "Hot path target",
                "constraints": "",
                "specification": {"body": "Return anything."},
                "evaluation_criteria": [
                    {"name": "Presence", "description": "A submission exists", "weight": 1}
                ],
            },
        )
    ).json()
    await client.post(f"/api/v1/challenges/{challenge['id']}/publish", headers=headers)
    return challenge["id"]


async def one_user_flow(
    client: httpx.AsyncClient, challenge_id: str, stats: Stats
) -> None:
    headers = {"X-User-Id": PARTICIPANT_ID}
    started = time.perf_counter()
    try:
        browse = await client.get("/api/v1/hackathons", headers=headers)
        browse.raise_for_status()
        if browse.json():
            hid = browse.json()[0]["id"]
            await client.get(f"/api/v1/hackathons/{hid}/challenges", headers=headers)
        detail = await client.get(f"/api/v1/challenges/{challenge_id}", headers=headers)
        detail.raise_for_status()
        created = await client.post(
            f"/api/v1/challenges/{challenge_id}/submissions",
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            json={"metadata": {"load": True}},
        )
        created.raise_for_status()
        status = await client.get(
            f"/api/v1/submissions/{created.json()['id']}", headers=headers
        )
        status.raise_for_status()
        stats.ok += 1
    except Exception:
        stats.err += 1
    finally:
        stats.latencies.append(time.perf_counter() - started)


async def run(args: argparse.Namespace) -> None:
    stats = Stats()
    limits = httpx.Limits(max_connections=args.connections, max_keepalive_connections=20)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=10, limits=limits) as client:
        health = await client.get("/health")
        health.raise_for_status()
        challenge_id = args.challenge_id or await ensure_challenge(client)
        interval = 1.0 / args.rate if args.rate > 0 else 0.05
        end = time.perf_counter() + args.duration
        in_flight: set[asyncio.Task] = set()

        async def spawn() -> None:
            task = asyncio.create_task(one_user_flow(client, challenge_id, stats))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)

        print(
            f"target={args.base_url} challenge={challenge_id} "
            f"rate={args.rate}/s duration={args.duration}s burst={args.burst}"
        )
        if args.burst:
            batch = max(args.rate, 1)
            while time.perf_counter() < end:
                for _ in range(batch):
                    await spawn()
                await asyncio.sleep(1.0)
        else:
            while time.perf_counter() < end:
                await spawn()
                await asyncio.sleep(interval)
        if in_flight:
            await asyncio.gather(*in_flight)
    n = len(stats.latencies) or 1
    ordered = sorted(stats.latencies)
    p95 = ordered[int(0.95 * (n - 1))]
    print(
        f"ok={stats.ok} err={stats.err} p95={p95:.3f}s "
        f"avg={sum(ordered) / n:.3f}s n={n}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="ChallengeForge slice 1 load hook")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--rate", type=int, default=20, help="Approximate flows per second")
    parser.add_argument("--duration", type=int, default=15, help="Seconds")
    parser.add_argument("--connections", type=int, default=50)
    parser.add_argument("--challenge-id", default="")
    parser.add_argument("--burst", action="store_true", help="Send `rate` flows each second")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

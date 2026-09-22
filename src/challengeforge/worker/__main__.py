"""CLI entry: python -m challengeforge.worker [--worker-id NAME]"""

from __future__ import annotations

import argparse
import asyncio

from challengeforge.config import get_settings
from challengeforge.worker import run_worker


def main() -> None:
    parser = argparse.ArgumentParser(description="ChallengeForge evaluation worker")
    parser.add_argument("--worker-id", default=None, help="Stable worker identity for logs")
    args = parser.parse_args()
    asyncio.run(run_worker(get_settings(), worker_id=args.worker_id))


if __name__ == "__main__":
    main()

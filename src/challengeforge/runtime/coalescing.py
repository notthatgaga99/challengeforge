"""In-process request coalescing for identical expensive computations.

Experiment / future LLM fan-in helper. Not used on the production evaluation
path unless explicitly opted in. No Redis required.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass
class CoalescingStats:
    submissions: int = 0
    computations: int = 0
    joins: int = 0
    failures: int = 0


@dataclass
class InProcessCoalescer:
    """One in-flight computation per key; waiters share the same Future."""

    _inflight: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    stats: CoalescingStats = field(default_factory=CoalescingStats)

    async def do(
        self, key: str, compute: Callable[[], Awaitable[T]]
    ) -> T:
        self.stats.submissions += 1
        existing = self._inflight.get(key)
        if existing is not None:
            self.stats.joins += 1
            return await asyncio.shield(existing)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[T] = loop.create_future()
        self._inflight[key] = fut
        self.stats.computations += 1
        try:
            result = await compute()
        except Exception as exc:
            self.stats.failures += 1
            fut.set_exception(exc)
            raise
        else:
            fut.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)

"""Opt-in per-request stage timings for interactive-path profiling.

Enabled only when Settings.request_profiling_enabled is true. Timings are
attached as the X-CF-Profile response header (JSON) so a local load generator
can aggregate server-side measurements without a tracing platform.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

_PROFILE: ContextVar["RequestProfile | None"] = ContextVar(
    "cf_request_profile", default=None
)


@dataclass
class SqlSample:
    category: str
    duration_ms: float
    rowcount: int | None = None


@dataclass
class RequestProfile:
    started_at: float = field(default_factory=time.perf_counter)
    pool_wait_ms: float = 0.0
    identity_ms: float = 0.0
    app_ms: float = 0.0
    serialize_ms: float = 0.0
    queue_position_ms: float = 0.0
    sql_samples: list[SqlSample] = field(default_factory=list)
    _sql_started: float | None = None
    _sql_category: str = "sql"

    def mark_sql_start(self, statement: str) -> None:
        self._sql_started = time.perf_counter()
        text = " ".join(statement.split()).lower()
        if "from users" in text:
            self._sql_category = "identity"
        elif "from evaluations" in text and "count(" in text:
            self._sql_category = "queue_position"
        elif "from evaluations" in text:
            self._sql_category = "evaluation"
        elif "from submissions" in text:
            self._sql_category = "submission"
        elif "from challenges" in text:
            self._sql_category = "challenge"
        elif "from hackathons" in text:
            self._sql_category = "hackathon"
        else:
            self._sql_category = "sql"

    def mark_sql_end(self, rowcount: int | None = None) -> None:
        if self._sql_started is None:
            return
        duration = (time.perf_counter() - self._sql_started) * 1000.0
        self.sql_samples.append(
            SqlSample(
                category=self._sql_category,
                duration_ms=round(duration, 3),
                rowcount=rowcount,
            )
        )
        self._sql_started = None

    def summary(self) -> dict[str, Any]:
        sql_ms = sum(sample.duration_ms for sample in self.sql_samples)
        by_category: dict[str, dict[str, float | int]] = {}
        for sample in self.sql_samples:
            entry = by_category.setdefault(
                sample.category, {"count": 0, "total_ms": 0.0}
            )
            entry["count"] = int(entry["count"]) + 1
            entry["total_ms"] = round(float(entry["total_ms"]) + sample.duration_ms, 3)
        total_ms = (time.perf_counter() - self.started_at) * 1000.0
        # Exclusive split: pool wait + SQL. Stage walls (identity/queue_position/
        # serialize/app) are inclusive overlays and may contain SQL time.
        exclusive = self.pool_wait_ms + sql_ms
        return {
            "total_ms": round(total_ms, 3),
            "pool_wait_ms": round(self.pool_wait_ms, 3),
            "sql_ms": round(sql_ms, 3),
            "query_count": len(self.sql_samples),
            "sql_by_category": by_category,
            "identity_ms": round(self.identity_ms, 3),
            "queue_position_ms": round(self.queue_position_ms, 3),
            "app_ms": round(self.app_ms, 3),
            "serialize_ms": round(self.serialize_ms, 3),
            "non_db_ms": round(max(0.0, total_ms - exclusive), 3),
            "accounting_note": (
                "pool_wait_ms and sql_ms are exclusive; identity/queue_position/"
                "serialize/app are inclusive stage walls that may include SQL"
            ),
        }

    def header_value(self) -> str:
        return json.dumps(self.summary(), separators=(",", ":"))


def current_profile() -> RequestProfile | None:
    return _PROFILE.get()


def start_profile() -> RequestProfile:
    profile = RequestProfile()
    _PROFILE.set(profile)
    return profile


def clear_profile() -> None:
    _PROFILE.set(None)


@contextmanager
def timed_stage(stage: str) -> Iterator[None]:
    profile = current_profile()
    if profile is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - started) * 1000.0
        if stage == "identity":
            profile.identity_ms += elapsed
        elif stage == "serialize":
            profile.serialize_ms += elapsed
        elif stage == "queue_position":
            profile.queue_position_ms += elapsed
        else:
            profile.app_ms += elapsed


def install_sqlalchemy_events(sync_engine: Any) -> None:
    """Attach cursor timing listeners once per engine."""
    from sqlalchemy import event

    if getattr(sync_engine, "_cf_profile_events", False):
        return

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _before_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        profile = current_profile()
        if profile is not None:
            profile.mark_sql_start(statement)

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _after_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        profile = current_profile()
        if profile is not None:
            rowcount = None
            try:
                rowcount = cursor.rowcount
            except Exception:
                rowcount = None
            profile.mark_sql_end(rowcount)

    sync_engine._cf_profile_events = True

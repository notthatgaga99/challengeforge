from typing import Any
from uuid import uuid4

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from challengeforge.request_profile import clear_profile, current_profile, start_profile
from challengeforge.runtime.interactive_signal import is_interactive_control_path

log = structlog.get_logger("challengeforge.http")


class RequestContextMiddleware:
    """Pure ASGI middleware so ContextVar profiles survive the request task."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        import time

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        request_id = headers.get("x-request-id") or str(uuid4())
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=scope.get("method"),
            path=scope.get("path"),
        )

        app = scope.get("app")
        settings = getattr(getattr(app, "state", None), "settings", None)
        profiling = bool(
            settings is not None and getattr(settings, "request_profiling_enabled", False)
        )
        feedback_enabled = bool(
            settings is not None
            and float(getattr(settings, "resource_interactive_p95_warn_ms", 0) or 0) > 0
            and float(getattr(settings, "resource_interactive_p95_critical_ms", 0) or 0)
            > 0
        )
        method = str(scope.get("method") or "GET")
        path = str(scope.get("path") or "")
        # Observe interactive wall time when profiling (baseline) or feedback is on.
        track_interactive = (
            (feedback_enabled or profiling)
            and is_interactive_control_path(method, path)
        )
        started = time.perf_counter()
        publisher = None
        if track_interactive and app is not None and settings is not None:
            from challengeforge.api.interactive_metrics import get_or_create_publisher

            publisher = get_or_create_publisher(app, settings)
            publisher.begin_request()

        if profiling:
            start_profile()

        status_code_box = {"value": 500}
        profile_header: str | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal profile_header
            if message["type"] == "http.response.start":
                status_code_box["value"] = int(message["status"])
                raw_headers = list(message.get("headers", []))
                raw_headers.append((b"x-request-id", request_id.encode("latin-1")))
                if profiling:
                    profile = current_profile()
                    if profile is not None:
                        profile_header = profile.header_value()
                        raw_headers.append(
                            (b"x-cf-profile", profile_header.encode("latin-1"))
                        )
                message = {**message, "headers": raw_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
            log.info("request_completed", status_code=status_code_box["value"])
        except Exception:
            log.exception("unhandled_exception")
            raise
        finally:
            if track_interactive and publisher is not None:
                total_ms = (time.perf_counter() - started) * 1000.0
                await self._record_interactive(publisher, total_ms)
            if profiling:
                clear_profile()

    async def _record_interactive(self, publisher: Any, total_ms: float) -> None:
        """Hot path: update in-memory windows. Persist only in sync publish mode."""
        pool_wait_ms: float | None = None
        profile = current_profile()
        if profile is not None:
            pool_wait_ms = float(profile.pool_wait_ms)
        try:
            publisher.record_wall(total_ms, pool_wait_ms=pool_wait_ms)
            if publisher.publish_mode == "sync" and publisher.should_persist_sync():
                await publisher.persist_now()
        except Exception:
            log.warning("interactive_hint_record_failed", exc_info=True)
        finally:
            publisher.end_request()

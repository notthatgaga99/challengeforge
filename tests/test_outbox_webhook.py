"""HTTP webhook NotificationSink (ADR 0021)."""

from __future__ import annotations

import json

import httpx
import pytest

from challengeforge.config import Settings
from challengeforge.outbox.events import evaluation_succeeded_key
from challengeforge.outbox.factory import resolve_notification_sink
from challengeforge.outbox.relay import OutboxRelay
from challengeforge.outbox.sinks import (
    DeliveredEvent,
    HttpWebhookNotificationSink,
    LoggingNotificationSink,
    SECRET_HEADER,
    WebhookDeliveryError,
)
from challengeforge.persistence.repositories import (
    EvaluationRepository,
    OutboxRepository,
)
from tests.test_outbox import _seed_running_evaluation
from tests.test_semantic_retrieval import _prepare


def test_resolve_sink_defaults_to_logging():
    sink = resolve_notification_sink(Settings(outbox_webhook_url=None))
    assert isinstance(sink, LoggingNotificationSink)
    sink2 = resolve_notification_sink(Settings(outbox_webhook_url="  "))
    assert isinstance(sink2, LoggingNotificationSink)


def test_resolve_sink_uses_webhook_when_url_set():
    sink = resolve_notification_sink(
        Settings(
            outbox_webhook_url="https://hooks.example/cf",
            outbox_webhook_timeout_seconds=2.5,
            outbox_webhook_secret="s3cret",
        )
    )
    assert isinstance(sink, HttpWebhookNotificationSink)
    assert sink.url == "https://hooks.example/cf"
    assert sink.timeout_seconds == 2.5
    assert sink.secret == "s3cret"


@pytest.mark.asyncio
async def test_http_webhook_posts_json_and_headers():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        sink = HttpWebhookNotificationSink(
            url="https://hooks.example/cf",
            secret="topsecret",
            client=client,
        )
        event = DeliveredEvent(
            event_id="e1",
            event_type="evaluation.succeeded",
            aggregate_type="evaluation",
            aggregate_id="a1",
            idempotency_key="eval-succeeded:a1",
            payload={"score": 90},
        )
        await sink.deliver(event)

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.headers["idempotency-key"] == "eval-succeeded:a1"
    assert req.headers[SECRET_HEADER.lower()] == "topsecret"
    body = json.loads(req.content.decode("utf-8"))
    assert body["event_id"] == "e1"
    assert body["event_type"] == "evaluation.succeeded"
    assert body["idempotency_key"] == "eval-succeeded:a1"
    assert body["payload"] == {"score": 90}


@pytest.mark.asyncio
async def test_http_webhook_raises_on_non_2xx():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        sink = HttpWebhookNotificationSink(
            url="https://hooks.example/cf", client=client
        )
        with pytest.raises(WebhookDeliveryError, match="500"):
            await sink.deliver(
                DeliveredEvent(
                    event_id="e1",
                    event_type="t",
                    aggregate_type="a",
                    aggregate_id="1",
                    idempotency_key="k",
                    payload={},
                )
            )


@pytest.mark.asyncio
async def test_relay_publishes_via_http_webhook():
    engine, factory = await _prepare()
    try:
        _h, _c, _s, ev_id = await _seed_running_evaluation(factory)
        async with factory() as session:
            await EvaluationRepository(session).mark_succeeded(
                ev_id, score=88, result_metadata={}, worker_id="w1"
            )
            await session.commit()

        posts: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(json.loads(request.content.decode("utf-8")))
            assert request.headers["idempotency-key"] == evaluation_succeeded_key(ev_id)
            return httpx.Response(202)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            sink = HttpWebhookNotificationSink(
                url="https://hooks.example/cf", client=client
            )
            assert await OutboxRelay(factory, sink=sink).drain_once() == 1

        assert len(posts) == 1
        assert posts[0]["payload"]["score"] == 88
        async with factory() as session:
            event = await OutboxRepository(session).get_by_idempotency_key(
                evaluation_succeeded_key(ev_id)
            )
            assert event is not None
            assert event.published_at is not None
            assert await OutboxRepository(session).count_pending() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_relay_retries_http_500_then_succeeds():
    engine, factory = await _prepare()
    try:
        _h, _c, _s, ev_id = await _seed_running_evaluation(factory)
        async with factory() as session:
            await EvaluationRepository(session).mark_succeeded(
                ev_id, score=50, result_metadata={}, worker_id="w1"
            )
            await session.commit()

        attempts = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(500, text="unavailable")
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            sink = HttpWebhookNotificationSink(
                url="https://hooks.example/cf", client=client
            )
            relay = OutboxRelay(factory, sink=sink)
            assert await relay.drain_once() == 0
            assert relay.failed == 1
            async with factory() as session:
                event = await OutboxRepository(session).get_by_idempotency_key(
                    evaluation_succeeded_key(ev_id)
                )
                assert event is not None
                assert event.published_at is None
                assert event.attempt_count == 1

            assert await relay.drain_once() == 1
            assert attempts["n"] == 2
            async with factory() as session:
                assert await OutboxRepository(session).count_pending() == 0
    finally:
        await engine.dispose()

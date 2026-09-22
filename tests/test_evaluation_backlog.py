from tests.conftest import participant_headers, published_challenge


async def test_evaluation_backlog_endpoint(client):
    challenge = await published_challenge(client)
    created = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers={**participant_headers(), "Idempotency-Key": "backlog-1"},
        json={"metadata": {"x": 1}},
    )
    submitted = await client.post(
        f"/api/v1/submissions/{created.json()['id']}/submit",
        headers=participant_headers(),
    )
    assert submitted.status_code == 200
    body = submitted.json()
    assert body["status"] == "submitted"
    assert body["evaluation_async"] is True
    assert body["evaluation_health"] in {"normal", "busy", "saturated", "critical"}
    assert body["evaluation_message"]

    backlog = await client.get(
        "/api/v1/evaluations/backlog", headers=participant_headers()
    )
    assert backlog.status_code == 200
    payload = backlog.json()
    assert payload["submissions_accepted"] is True
    assert payload["queued_count"] >= 1
    assert payload["estimate_is_approximate"] is True
    assert "message" in payload
    assert "light_queued_count" not in payload
    assert "worker_capacity" not in payload

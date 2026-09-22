from uuid import uuid4

from tests.conftest import (
    organizer_headers,
    participant_headers,
    participant2_headers,
    published_challenge,
)


async def test_invalid_challenge_rejected(client):
    hackathon = (
        await client.post(
            "/api/v1/hackathons",
            headers=organizer_headers(),
            json={"title": "Cup", "description": "Event"},
        )
    ).json()
    response = await client.post(
        f"/api/v1/hackathons/{hackathon['id']}/challenges",
        headers=organizer_headers(),
        json={
            "title": " ",
            "description": "ok",
            "specification": {"body": "spec"},
            "evaluation_criteria": [],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"

    empty_spec = await client.post(
        f"/api/v1/hackathons/{hackathon['id']}/challenges",
        headers=organizer_headers(),
        json={
            "title": "Valid title",
            "description": "ok",
            "specification": {"body": "  "},
            "evaluation_criteria": [],
        },
    )
    assert empty_spec.status_code == 422


async def test_publish_without_criteria_rejected(client):
    hackathon = (
        await client.post(
            "/api/v1/hackathons",
            headers=organizer_headers(),
            json={"title": "Cup", "description": "Event"},
        )
    ).json()
    challenge = (
        await client.post(
            f"/api/v1/hackathons/{hackathon['id']}/challenges",
            headers=organizer_headers(),
            json={
                "title": "No criteria",
                "description": "ok",
                "specification": {"body": "A spec"},
                "evaluation_criteria": [],
            },
        )
    ).json()
    published = await client.post(
        f"/api/v1/challenges/{challenge['id']}/publish",
        headers=organizer_headers(),
    )
    assert published.status_code == 422


async def test_submission_for_nonexistent_challenge_rejected(client):
    response = await client.post(
        f"/api/v1/challenges/{uuid4()}/submissions",
        headers=participant_headers(),
        json={"metadata": {}},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_invalid_submission_state_transition_rejected(client):
    challenge = await published_challenge(client)
    created = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers=participant_headers(),
        json={"metadata": {}},
    )
    submission_id = created.json()["id"]
    cancelled = await client.post(
        f"/api/v1/submissions/{submission_id}/cancel",
        headers=participant_headers(),
    )
    assert cancelled.status_code == 200
    again = await client.post(
        f"/api/v1/submissions/{submission_id}/submit",
        headers=participant_headers(),
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_state_transition"


async def test_duplicate_idempotency_key_replays(client):
    challenge = await published_challenge(client)
    headers = {**participant_headers(), "Idempotency-Key": "same-click"}
    first = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers=headers,
        json={"metadata": {"n": 1}},
    )
    second = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers=headers,
        json={"metadata": {"n": 1}},
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["idempotent_replay"] is True

    third = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers=participant_headers(),
        json={"metadata": {"n": 2}},
    )
    assert third.status_code == 201
    assert third.json()["id"] != first.json()["id"]


async def test_idempotency_key_with_different_payload_conflicts(client):
    challenge = await published_challenge(client)
    headers = {**participant_headers(), "Idempotency-Key": "payload-conflict"}
    first = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers=headers,
        json={"metadata": {"revision": 1}},
    )
    conflict = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers=headers,
        json={"metadata": {"revision": 2}},
    )
    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"
    listed = await client.get(
        f"/api/v1/users/{first.json()['participant_id']}/submissions",
        headers=participant_headers(),
    )
    assert len(listed.json()) == 1


async def test_participant_wide_key_conflicts_across_challenges(client):
    first_challenge = await published_challenge(client)
    second_challenge = await published_challenge(client)
    headers = {**participant_headers(), "Idempotency-Key": "cross-challenge"}
    first = await client.post(
        f"/api/v1/challenges/{first_challenge['id']}/submissions",
        headers=headers,
        json={"metadata": {"same": True}},
    )
    conflict = await client.post(
        f"/api/v1/challenges/{second_challenge['id']}/submissions",
        headers=headers,
        json={"metadata": {"same": True}},
    )
    assert first.status_code == 201
    assert conflict.status_code == 409


async def test_close_rejects_new_submission(client):
    challenge = await published_challenge(client)
    closed = await client.post(
        f"/api/v1/challenges/{challenge['id']}/close",
        headers=organizer_headers(),
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"

    rejected = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers={
            **participant_headers(),
            "Idempotency-Key": "after-close",
        },
        json={"metadata": {}},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "conflict"


async def test_unauthorized_organizer_and_participant_operations(client):
    missing = await client.post(
        "/api/v1/hackathons", json={"title": "X", "description": "Y"}
    )
    assert missing.status_code == 401

    as_participant = await client.post(
        "/api/v1/hackathons",
        headers=participant_headers(),
        json={"title": "X", "description": "Y"},
    )
    assert as_participant.status_code == 403
    assert as_participant.json()["error"]["code"] == "permission_denied"

    challenge = await published_challenge(client)
    as_organizer = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers=organizer_headers(),
        json={"metadata": {}},
    )
    assert as_organizer.status_code == 403

    created = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers=participant_headers(),
        json={"metadata": {"private": True}},
    )
    listed = await client.get(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers=participant_headers(),
    )
    assert listed.status_code == 403

    other = await client.get(
        f"/api/v1/submissions/{created.json()['id']}",
        headers=participant2_headers(),
    )
    assert other.status_code == 403

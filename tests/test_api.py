from tests.conftest import organizer_headers, participant_headers, published_challenge


async def test_create_and_get_hackathon(client):
    created = await client.post(
        "/api/v1/hackathons",
        headers=organizer_headers(),
        json={"title": "Autumn Hack", "description": "A weekend event."},
    )
    assert created.status_code == 201
    body = created.json()
    fetched = await client.get(
        f"/api/v1/hackathons/{body['id']}", headers=organizer_headers()
    )
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Autumn Hack"
    assert fetched.json()["status"] == "draft"


async def test_create_and_retrieve_challenge(client):
    hackathon = (
        await client.post(
            "/api/v1/hackathons",
            headers=organizer_headers(),
            json={"title": "Cup", "description": "Event"},
        )
    ).json()
    created = await client.post(
        f"/api/v1/hackathons/{hackathon['id']}/challenges",
        headers=organizer_headers(),
        json={
            "title": "Cache design",
            "description": "Sketch an LRU cache.",
            "constraints": "O(1) get/put",
            "specification": {"body": "Implement get and put."},
            "evaluation_criteria": [
                {"name": "Complexity", "description": "O(1) operations", "weight": 2}
            ],
        },
    )
    assert created.status_code == 201
    challenge_id = created.json()["id"]
    fetched = await client.get(
        f"/api/v1/challenges/{challenge_id}", headers=organizer_headers()
    )
    assert fetched.status_code == 200
    assert fetched.json()["specification"]["body"] == "Implement get and put."
    assert fetched.json()["evaluation_criteria"][0]["name"] == "Complexity"


async def test_create_retrieve_and_list_submissions(client):
    challenge = await published_challenge(client)
    created = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers={**participant_headers(), "Idempotency-Key": "attempt-1"},
        json={"metadata": {"repo": "https://example.com/repo"}},
    )
    assert created.status_code == 201
    submission_id = created.json()["id"]
    assert created.json()["status"] == "created"
    assert created.json()["idempotent_replay"] is False

    fetched = await client.get(
        f"/api/v1/submissions/{submission_id}", headers=participant_headers()
    )
    assert fetched.status_code == 200
    assert fetched.json()["metadata"]["repo"] == "https://example.com/repo"

    mine = await client.get(
        f"/api/v1/users/{fetched.json()['participant_id']}/submissions",
        headers=participant_headers(),
    )
    assert mine.status_code == 200
    assert mine.json()[0]["id"] == submission_id

    listed = await client.get(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers=organizer_headers(),
    )
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == submission_id


async def test_health_and_request_id(client):
    response = await client.get("/health", headers={"X-Request-ID": "req-123"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-123"


async def test_ui_home_renders(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "ChallengeForge" in response.text

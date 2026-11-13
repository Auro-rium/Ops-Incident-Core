"""Production-path checks for the queue CI leg: live API, Redis, and a running worker."""

from __future__ import annotations

import os
import time

import httpx
import pytest
import redis

pytestmark = pytest.mark.skipif(
    os.getenv("WORKER_MODE") != "queue",
    reason="Needs WORKER_MODE=queue with a running worker",
)

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
POLL_TIMEOUT_SECONDS = 120


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE_URL, timeout=60.0) as http_client:
        yield http_client


@pytest.fixture
def headers(client):
    response = client.post(
        "/v1/auth/login",
        json={"email": "admin@incidentops.local", "password": "incidentops"},
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def project_id(client, headers):
    response = client.post(
        "/v1/projects",
        headers=headers,
        json={"name": f"queue-{os.urandom(4).hex()}", "demo_mode": False},
    )
    response.raise_for_status()
    return response.json()["project_id"]


def _poll(client, path, headers, pending):
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    payload = {}
    while time.time() < deadline:
        response = client.get(path, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") not in pending:
            return payload
        time.sleep(2)
    pytest.fail(f"{path} still pending after {POLL_TIMEOUT_SECONDS}s: {payload.get('status')}")


def test_worker_completes_a_queued_eval(client, headers, project_id):
    response = client.post(
        "/v1/evals/run",
        headers=headers,
        json={
            "project_id": project_id,
            "top_k": 3,
            "cases": [
                {
                    "id": "queue_case",
                    "question": "What changed?",
                    "expected_terms": ["timeout"],
                    "forbidden_terms": [],
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    final = _poll(client, f"/v1/evals/{body['eval_run_id']}", headers, {"queued", "running"})
    assert final["status"] == "completed"


def test_worker_picks_up_a_queued_run(client, headers, project_id):
    response = client.post(
        "/v1/runs",
        headers=headers,
        json={"project_id": project_id, "query": "What changed?", "top_k": 1, "create_issue_draft": False},
    )
    assert response.status_code == 200
    body = response.json()
    final = _poll(client, f"/v1/runs/{body['run_id']}", headers, {"queued", "running"})
    assert final["status"] in {"completed", "awaiting_approval"}


def test_rate_limits_are_stored_in_redis(client, headers):
    # The login fixture above went through the limiter.
    redis_client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    assert any(redis_client.scan_iter(match="incidentops:rate:*", count=1000))

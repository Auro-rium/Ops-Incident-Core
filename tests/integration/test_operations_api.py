from __future__ import annotations

import os

import httpx

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")


def _admin_client() -> tuple[httpx.Client, dict[str, str]]:
    client = httpx.Client(base_url=BASE_URL, timeout=60.0)
    login = client.post("/v1/auth/login", json={"email": "admin@incidentops.local", "password": "incidentops"})
    login.raise_for_status()
    return client, {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_observer_and_logging_runs_are_project_scoped_and_idempotent():
    client, headers = _admin_client()
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"name": f"operations-{os.urandom(4).hex()}", "demo_mode": True},
    )
    project.raise_for_status()
    project_id = project.json()["project_id"]

    first = client.post(
        f"/v1/projects/{project_id}/operations/observer/runs",
        headers=headers,
        json={"idempotency_key": "observer-smoke"},
    )
    assert first.status_code == 202
    assert first.json()["run_type"] == "observer"
    assert first.json()["status"] == "completed"

    repeated = client.post(
        f"/v1/projects/{project_id}/operations/observer/runs",
        headers=headers,
        json={"idempotency_key": "observer-smoke"},
    )
    assert repeated.status_code == 202
    assert repeated.json()["operational_run_id"] == first.json()["operational_run_id"]

    logging_run = client.post(
        f"/v1/projects/{project_id}/operations/logging/runs",
        headers=headers,
        json={"idempotency_key": "logging-smoke"},
    )
    assert logging_run.status_code == 202
    assert logging_run.json()["status"] == "completed"

    runs = client.get(f"/v1/projects/{project_id}/operations/runs", headers=headers)
    assert runs.status_code == 200
    assert {item["run_type"] for item in runs.json()} == {"observer", "logging"}

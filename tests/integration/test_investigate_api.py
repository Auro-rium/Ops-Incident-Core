from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
FIXTURE_PATH = str((Path(__file__).resolve().parents[1] / "fixtures" / "basic_incident").resolve())


@pytest.fixture(scope="module")
def project_id():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    login = client.post("/v1/auth/login", json={"email": "admin@incidentops.local", "password": "incidentops"})
    login.raise_for_status()
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = client.post(
        "/v1/projects",
        headers=headers,
        json={"name": f"investigate-{os.urandom(4).hex()}", "demo_mode": True},
    )
    project_id = response.json()["project_id"]
    client.post(f"/v1/projects/{project_id}/ingest", headers=headers, json={"path": FIXTURE_PATH})
    return project_id


def test_investigate_returns_generic_shape(project_id):
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    response = client.post(
        "/v1/investigate",
        json={"project_id": project_id, "query": "Why did GET /v1/orders slow down after deploy abc1234?", "top_k": 8},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["task_type"] == "latency_investigation"
    assert payload["query_intent"] == "root_cause_investigation"
    assert payload["evidence"]
    assert "likely_root_cause" in payload
    assert payload["confidence"] in {"low", "medium", "high"}
    assert isinstance(payload["confidence_reasons"], list)
    assert isinstance(payload["missing_data"], list)
    assert payload["confidence"] == payload["likely_root_cause"]["confidence"]


def test_investigate_debug_returns_diagnostics(project_id):
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    response = client.post(
        "/v1/investigate",
        json={
            "project_id": project_id,
            "query": "Why did GET /v1/orders slow down after deploy abc1234?",
            "top_k": 8,
            "debug": True,
        },
    )
    assert response.status_code == 200
    debug = response.json()["debug"]
    assert debug["vector_candidates_count"] >= 0
    assert debug["lexical_candidates_count"] >= 0
    assert debug["query_intent"]["intent"] == "root_cause_investigation"
    assert "investigation_supported" in debug
    assert "applied_filters" in debug
    assert "metadata_boosts_used" in debug
    assert "top_rejected" in debug

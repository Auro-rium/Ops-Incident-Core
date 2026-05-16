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
        json={"name": f"retrieval-{os.urandom(4).hex()}", "demo_mode": True},
    )
    project_id = response.json()["project_id"]
    client.post(f"/v1/projects/{project_id}/ingest", headers=headers, json={"path": FIXTURE_PATH})
    return project_id


def test_retrieval_returns_log_or_deploy_evidence(project_id):
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    response = client.post(
        "/v1/search",
        json={"project_id": project_id, "query": "Why did GET /v1/orders slow down after deploy abc1234?", "top_k": 8},
    )
    paths = [item["document_path"] for item in response.json()["results"]]
    assert any("api.log" in path for path in paths)
    assert any("deploy-history.json" in path or "diff-abc1234.patch" in path for path in paths)

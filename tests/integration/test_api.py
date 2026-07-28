from __future__ import annotations

import os
import json
from pathlib import Path

import httpx
import pytest

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
FIXTURE_PATH = str((Path(__file__).resolve().parents[1] / "fixtures" / "basic_incident").resolve())


@pytest.fixture
def client():
    return httpx.Client(base_url=BASE_URL, timeout=120.0)


def create_fixture_project(client: httpx.Client) -> str:
    headers = login(client)
    response = client.post(
        "/v1/projects",
        headers=headers,
        json={"name": f"fixture-{os.urandom(4).hex()}", "demo_mode": True},
    )
    project_id = response.json()["project_id"]
    ingest = client.post(f"/v1/projects/{project_id}/ingest", headers=headers, json={"path": FIXTURE_PATH})
    assert ingest.status_code == 200
    return project_id


def login(client: httpx.Client) -> dict[str, str]:
    response = client.post("/v1/auth/login", json={"email": "admin@incidentops.local", "password": "incidentops"})
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def create_authenticated_fixture_project(client: httpx.Client) -> tuple[str, dict[str, str]]:
    headers = login(client)
    response = client.post(
        "/v1/projects",
        headers=headers,
        json={"name": f"fixture-auth-{os.urandom(4).hex()}", "demo_mode": False},
    )
    response.raise_for_status()
    project_id = response.json()["project_id"]
    ingest = client.post(f"/v1/projects/{project_id}/ingest", headers=headers, json={"path": FIXTURE_PATH})
    assert ingest.status_code == 200
    return project_id, headers


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_ready_returns_ready_on_migrated_database(self, client):
        response = client.get("/ready")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ready"
        assert payload["database"] == "ok"
        assert payload["vector_store"] == "ok"
        assert payload["required_tables"] == "ok"
        assert payload["migration"] == "ok"


class TestIngestEndpoint:
    def test_project_listing_is_authenticated_and_membership_scoped(self, client):
        headers = login(client)
        created = client.post(
            "/v1/projects",
            headers=headers,
            json={"name": f"project-list-{os.urandom(4).hex()}", "demo_mode": False},
        )
        assert created.status_code == 201

        assert client.get("/v1/projects").status_code == 401
        response = client.get("/v1/projects", headers=headers)
        assert response.status_code == 200
        project = next(item for item in response.json() if item["project_id"] == created.json()["project_id"])
        assert project["role"] == "admin"

    def test_ingest_fixture_data(self, client):
        headers = login(client)
        response = client.post(
            "/v1/projects",
            headers=headers,
            json={"name": f"ingest-{os.urandom(4).hex()}", "demo_mode": True},
        )
        project_id = response.json()["project_id"]
        ingest = client.post(f"/v1/projects/{project_id}/ingest", headers=headers, json={"path": FIXTURE_PATH})
        assert ingest.status_code == 200
        payload = ingest.json()
        assert payload["documents_ingested"] > 0
        assert payload["chunks_created"] > 0
        assert payload["total_files_seen"] >= payload["files_ingested"]
        assert "source_coverage" in payload
        assert "source_type_counts" in payload
        assert "chunk_type_counts" in payload

    def test_ingest_reports_skips_and_parser_errors(self, client, tmp_path):
        (tmp_path / "notes.md").write_text("# Notes\nUseful content\n", encoding="utf-8")
        (tmp_path / "bad-deploy-history.json").write_text("{not valid json", encoding="utf-8")
        (tmp_path / "ignore.bin").write_bytes(b"\x00\x01\x02")
        (tmp_path / "huge.log").write_text("x" * 2_100_000, encoding="utf-8")
        headers = login(client)
        response = client.post(
            "/v1/projects",
            headers=headers,
            json={"name": f"diag-{os.urandom(4).hex()}", "demo_mode": True},
        )
        project_id = response.json()["project_id"]
        ingest = client.post(f"/v1/projects/{project_id}/ingest", headers=headers, json={"path": str(tmp_path)})
        assert ingest.status_code == 200
        payload = ingest.json()
        assert payload["chunks_created"] > 0
        assert payload["files_skipped"] >= 2
        assert any(item["reason"] == "unsupported_extension" for item in payload["skipped_files"])
        assert any(item["reason"] == "file_too_large" for item in payload["skipped_files"])
        assert any(item["path"] == "bad-deploy-history.json" for item in payload["parser_errors"])

    def test_ingest_coverage_warnings_when_sources_missing(self, client, tmp_path):
        (tmp_path / "docs" / "guide.md").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "docs" / "guide.md").write_text("# Guide\nOnly docs here\n", encoding="utf-8")
        headers = login(client)
        response = client.post(
            "/v1/projects",
            headers=headers,
            json={"name": f"coverage-{os.urandom(4).hex()}", "demo_mode": True},
        )
        project_id = response.json()["project_id"]
        ingest = client.post(f"/v1/projects/{project_id}/ingest", headers=headers, json={"path": str(tmp_path)})
        assert ingest.status_code == 200
        coverage = ingest.json()["source_coverage"]
        assert coverage["has_runbooks"] is True
        assert coverage["has_deploys"] is False
        assert coverage["has_incidents"] is False
        assert any("Deploy-regression" in warning for warning in coverage["warnings"])


class TestSearchAndAnswer:
    def test_readiness_reports_fixture_coverage(self, client):
        project_id, headers = create_authenticated_fixture_project(client)
        response = client.get(f"/v1/projects/{project_id}/readiness", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["project_id"] == project_id
        assert payload["score"] > 0
        assert payload["grade"] in {"weak", "partial", "good", "excellent"}
        assert payload["coverage"]["has_code"] is True
        assert payload["coverage"]["has_logs"] is True
        assert payload["coverage"]["has_deploys"] is True
        assert payload["counts"]["documents"] > 0
        assert payload["counts"]["chunks"] > 0
        assert payload["latest_sync"]["status"] in {"success", "partial_success"}
        assert payload["answerable_questions"]
        assert "suggested_actions" in payload

    def test_readiness_requires_project_membership(self, client):
        project_id, _headers = create_authenticated_fixture_project(client)
        response = client.get(f"/v1/projects/{project_id}/readiness")
        assert response.status_code == 401

    def test_search_returns_fixture_results(self, client):
        project_id = create_fixture_project(client)
        response = client.post(
            "/v1/search",
            json={"project_id": project_id, "query": "Why did GET /v1/orders slow down after deploy abc1234?", "top_k": 8},
        )
        assert response.status_code == 200
        assert response.json()["total"] > 0

    def test_search_debug_returns_diagnostics(self, client):
        project_id = create_fixture_project(client)
        response = client.post(
            "/v1/search",
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
        assert "merged_candidates_count" in debug
        assert debug["query_intent"]["intent"] == "runtime_incident"
        assert "source_type_distribution" in debug
        assert "chunk_type_distribution" in debug
        assert "applied_boosts" in debug
        assert "applied_penalties" in debug
        assert "retrieval_branch_latencies" in debug
        assert "total_retrieval_latency_ms" in debug
        assert "metadata_boosts_used" in debug
        assert "evidence_mix" in debug
        assert "top_rejected" in debug

    def test_answer_returns_evidence_without_llm(self, client):
        project_id = create_fixture_project(client)
        response = client.post(
            "/v1/answer",
            json={"project_id": project_id, "query": "Why did GET /v1/orders slow down after deploy abc1234?", "top_k": 8},
        )
        assert response.status_code == 200
        assert response.json()["query_intent"] == "runtime_incident"
        assert "warnings" in response.json()
        assert response.json()["evidence"]

    def test_answer_uses_direct_evidence_mode_for_code_lookup(self, client):
        project_id = create_fixture_project(client)
        response = client.post(
            "/v1/answer",
            json={"project_id": project_id, "query": "Where is handle_orders implemented?", "top_k": 5},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["query_intent"] == "code_location"
        assert payload["synthesis_mode"] == "direct_evidence"
        assert payload["answer"]["confidence"] == "high"
        assert "services/api/service.py" in (payload["answer"]["answer_text"] or "")
        assert any(item["chunk_type"] in {"function", "python_function"} for item in payload["evidence"])


class TestEvalEndpoint:
    def test_eval_accepts_custom_cases_path(self, client, tmp_path):
        project_id, headers = create_authenticated_fixture_project(client)
        cases_path = tmp_path / "custom_cases.jsonl"
        cases_path.write_text(
            json.dumps(
                {
                    "id": "case_001",
                    "question": "Why did GET /v1/orders slow down after deploy abc1234?",
                    "expected_documents": ["deploy-history.json", "api.log"],
                    "expected_terms": ["deploy", "timeout"],
                    "forbidden_terms": ["database outage"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        response = client.post(
            "/v1/evals/run",
            headers=headers,
            json={"project_id": project_id, "cases_path": str(cases_path), "top_k": 8},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["summary"]["cases"] == 1
        assert "avg_recall" in payload["summary"]
        assert "avg_term_coverage" in payload["summary"]

from __future__ import annotations

import os
import subprocess
import sys
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
        assert payload["pgvector"] == "ok"
        assert payload["required_tables"] == "ok"
        assert payload["migration"] == "ok"


class TestIngestEndpoint:
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
        assert "metadata_boosts_used" in debug
        assert "top_rejected" in debug

    def test_answer_returns_evidence_without_llm(self, client):
        project_id = create_fixture_project(client)
        response = client.post(
            "/v1/answer",
            json={"project_id": project_id, "query": "Why did GET /v1/orders slow down after deploy abc1234?", "top_k": 8},
        )
        assert response.status_code == 200
        assert response.json()["evidence"]


class TestSmokeScript:
    def test_smoke_script_exits_non_zero_on_zero_chunks(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                "scripts/smoke_local.py",
                "--base-url",
                BASE_URL,
                "--data-path",
                str(empty_dir),
                "--query",
                "Why did latency increase after the last deploy?",
            ],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


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

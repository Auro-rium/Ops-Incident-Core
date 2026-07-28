from __future__ import annotations

import hashlib
import os
import asyncio
import uuid
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from incidentops.config.settings import get_settings
from incidentops.db.models import AuditEvent, Chunk, Document, EvidenceRelation

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
FIXTURE_ROOT = (Path(__file__).resolve().parents[1] / "fixtures" / "basic_incident").resolve()


def _login(client: httpx.Client) -> dict[str, str]:
    response = client.post(
        "/v1/auth/login",
        json={"email": "admin@incidentops.local", "password": "incidentops"},
    )
    response.raise_for_status()
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_project(client: httpx.Client, headers: dict[str, str], name_prefix: str = "collector") -> str:
    response = client.post(
        "/v1/projects",
        headers=headers,
        json={"name": f"{name_prefix}-{os.urandom(4).hex()}", "demo_mode": False},
    )
    response.raise_for_status()
    return response.json()["project_id"]


def _build_fixture_documents() -> list[dict]:
    source_types = {
        "deploys/deploy-history.json": "deploy",
        "deploys/diff-abc1234.patch": "deploy",
        "docs/runbook.md": "runbook",
        "incidents/previous.md": "incident",
        "logs/api.log": "logs",
        "services/api/service.py": "code",
    }
    documents = []
    for relative_path, source_type in source_types.items():
        path = FIXTURE_ROOT / relative_path
        content = path.read_text(encoding="utf-8")
        documents.append(
            {
                "external_id": relative_path,
                "path": relative_path,
                "source_type": source_type,
                "content": content,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "metadata": {"service_name": "orders" if "orders" in content.lower() else "api"},
                "size_bytes": path.stat().st_size,
                "modified_at": "2026-05-05T10:00:00Z",
            }
        )
    return documents


def _build_repeated_log_documents(count: int) -> list[dict]:
    path = FIXTURE_ROOT / "logs" / "api.log"
    content = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return [
        {
            "external_id": f"logs/api-{index}.log",
            "path": f"logs/api-{index}.log",
            "source_type": "logs",
            "content": content,
            "content_hash": digest,
            "metadata": {"service_name": "api"},
            "size_bytes": path.stat().st_size,
            "modified_at": "2026-05-05T10:00:00Z",
        }
        for index in range(count)
    ]


async def _document_chunk_count(project_id: str, source_id: str, external_id: str) -> int:
    factory, engine = _test_session_factory()
    async with factory() as db:
        result = await db.execute(
            select(Document).where(
                Document.project_id == uuid.UUID(project_id),
                Document.source_id == uuid.UUID(source_id),
                Document.external_id == external_id,
            )
        )
        document = result.scalar_one_or_none()
        if not document:
            return 0
        count_result = await db.execute(select(func.count()).select_from(Chunk).where(Chunk.document_id == document.id))
        count = int(count_result.scalar_one())
    await engine.dispose()
    return count


async def _audit_count(action: str) -> int:
    factory, engine = _test_session_factory()
    async with factory() as db:
        result = await db.execute(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == action))
        count = int(result.scalar_one())
    await engine.dispose()
    return count


async def _project_relation_count(project_id: str) -> int:
    factory, engine = _test_session_factory()
    async with factory() as db:
        result = await db.execute(
            select(func.count()).select_from(EvidenceRelation).where(EvidenceRelation.project_id == uuid.UUID(project_id))
        )
        count = int(result.scalar_one())
    await engine.dispose()
    return count


def _test_session_factory():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False), engine


def test_capabilities_endpoint_returns_collector_contract():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    response = client.get("/v1/capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "0.5.0"
    assert payload["features"]["sync_tracking"] is True
    assert payload["features"]["collector_registration"] is True
    assert payload["features"]["batch_ingest"] is True
    assert payload["features"]["search"] is True
    assert payload["features"]["investigate"] is True
    assert payload["features"]["runs"] is True
    assert payload["limits"]["max_documents_per_batch"] == get_settings().max_documents_per_batch
    assert payload["limits"]["max_document_bytes"] == get_settings().max_document_bytes
    assert payload["limits"]["max_batch_bytes"] == get_settings().max_batch_bytes
    assert payload["limits"]["max_batch_size"] == get_settings().max_batch_bytes
    assert payload["endpoints"]["batch_upload"] == "/v1/sources/{source_id}/documents/batch"
    assert payload["endpoints"]["register_collector"] == "/v1/projects/{project_id}/collectors/register"
    assert payload["endpoints"]["run_events"] == "/v1/runs/{run_id}/events"


def test_source_registry_and_collector_sync_batch_ingest_flow():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    project_id = _create_project(client, headers)

    create_source = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={
            "name": "orders logs",
            "source_type": "filesystem",
            "sync_mode": "manual",
            "config": {"description": "Production logs exported locally"},
        },
    )
    assert create_source.status_code == 201
    source_id = create_source.json()["id"]
    repeated_create_source = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={
            "name": "orders logs",
            "source_type": "filesystem",
            "sync_mode": "manual",
            "config": {"description": "Production logs exported locally"},
        },
    )
    assert repeated_create_source.status_code == 201
    assert repeated_create_source.json()["id"] == source_id

    list_sources = client.get(f"/v1/projects/{project_id}/sources", headers=headers)
    assert list_sources.status_code == 200
    assert any(item["id"] == source_id for item in list_sources.json())

    register_1 = client.post(
        f"/v1/projects/{project_id}/collectors/register",
        headers=headers,
        json={"name": "local-collector", "environment": "dev", "version": "0.1.0"},
    )
    register_2 = client.post(
        f"/v1/projects/{project_id}/collectors/register",
        headers=headers,
        json={"name": "local-collector", "environment": "dev", "version": "0.1.1"},
    )
    assert register_1.status_code == 200
    assert register_2.status_code == 200
    collector_id = register_1.json()["collector_id"]
    assert register_2.json()["collector_id"] == collector_id

    start_sync = client.post(
        f"/v1/sources/{source_id}/syncs/start",
        headers=headers,
        json={"collector_id": collector_id, "diagnostics": {"total_files_seen": 6}},
    )
    assert start_sync.status_code == 200
    sync_id = start_sync.json()["sync_id"]

    batch = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=headers,
        json={
            "sync_id": sync_id,
            "collector_id": collector_id,
            "collector_version": "0.1.1",
            "schema_version": "normalized-document-v1",
            "core_api_version": "0.5.0",
            "documents": _build_fixture_documents(),
        },
    )
    assert batch.status_code == 200
    batch_payload = batch.json()
    assert batch_payload["received"] == 6
    assert batch_payload["created"] == 6
    assert batch_payload["chunks_created"] > 0
    assert batch_payload["errors"] == []
    assert batch_payload["diagnostics"]["collector_version"] == "0.1.1"
    assert batch_payload["diagnostics"]["schema_version"] == "normalized-document-v1"
    assert batch_payload["diagnostics"]["core_api_version"] == "0.5.0"
    assert batch_payload["diagnostics"]["last_batch_parser_error_reasons"] == {}
    assert batch_payload["diagnostics"]["last_batch_chunk_discard_reasons"].get("chunk_limit_exceeded", 0) == 0
    assert batch_payload["diagnostics"]["last_batch_embedding_failures"] == 0
    assert asyncio.run(_project_relation_count(project_id)) > 0

    finish = client.post(
        f"/v1/sources/{source_id}/syncs/{sync_id}/finish",
        headers=headers,
        json={
            "status": "success",
            "diagnostics": {"files_seen": 6, "files_skipped": 0, "parser_errors": 0},
            "coverage": {
                "has_logs": True,
                "has_code": True,
                "has_deploys": True,
                "has_incidents": True,
                "has_runbooks": True,
                "warnings": [],
            },
        },
    )
    assert finish.status_code == 200
    assert finish.json()["status"] == "success"

    latest_sync = client.get(f"/v1/sources/{source_id}/syncs/latest", headers=headers)
    assert latest_sync.status_code == 200
    latest_payload = latest_sync.json()
    assert latest_payload["sync_id"] == sync_id
    assert latest_payload["status"] == "success"
    assert latest_payload["documents_received"] == 6
    assert latest_payload["chunks_created"] > 0
    assert latest_payload["diagnostics"]["collector_version"] == "0.1.1"
    assert latest_payload["diagnostics"]["schema_version"] == "normalized-document-v1"
    assert latest_payload["diagnostics"]["core_api_version"] == "0.5.0"

    listed_sources = client.get(f"/v1/projects/{project_id}/sources", headers=headers)
    source_payload = next(item for item in listed_sources.json() if item["id"] == source_id)
    assert source_payload["last_sync_status"] == "success"

    search = client.post(
        "/v1/search",
        headers=headers,
        json={"project_id": project_id, "query": "Why did GET /v1/orders slow down after deploy abc1234?", "top_k": 8},
    )
    assert search.status_code == 200
    assert search.json()["total"] > 0

    architecture_search = client.post(
        "/v1/search",
        headers=headers,
        json={
            "project_id": project_id,
            "query": "How is the docs architecture organized?",
            "top_k": 8,
            "debug": True,
        },
    )
    assert architecture_search.status_code == 200
    architecture_debug = architecture_search.json()["debug"]
    assert architecture_debug["query_intent"]["intent"] == "architecture"
    assert architecture_debug["graph_candidates_count"] > 0
    assert architecture_debug["fusion"]["method"] == "weighted_reciprocal_rank_fusion"

    investigate = client.post(
        "/v1/investigate",
        headers=headers,
        json={"project_id": project_id, "query": "Why did GET /v1/orders slow down after deploy abc1234?", "top_k": 8},
    )
    assert investigate.status_code == 200
    assert investigate.json()["evidence"]


def test_collector_batch_ingest_allows_many_batches_without_human_request_limit():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    project_id = _create_project(client, headers, "collector-batch-limit")
    source_id = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={"name": "batch-limit", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    ).json()["id"]
    collector_id = client.post(
        f"/v1/projects/{project_id}/collectors/register",
        headers=headers,
        json={"name": "limit-collector", "environment": "dev", "version": "0.1.0"},
    ).json()["collector_id"]
    sync_id = client.post(
        f"/v1/sources/{source_id}/syncs/start",
        headers=headers,
        json={"collector_id": collector_id, "diagnostics": {"total_files_seen": 30}},
    ).json()["sync_id"]

    documents = _build_repeated_log_documents(30)
    for document in documents:
        response = client.post(
            f"/v1/sources/{source_id}/documents/batch",
            headers=headers,
            json={"sync_id": sync_id, "collector_id": collector_id, "documents": [document]},
        )
        assert response.status_code == 200

    latest_sync = client.get(f"/v1/sources/{source_id}/syncs/latest", headers=headers)
    assert latest_sync.status_code == 200
    assert latest_sync.json()["documents_received"] == 30


def test_batch_ingest_skips_unchanged_and_updates_changed_content():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    project_id = _create_project(client, headers, "idempotent")
    source_id = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={"name": "stream", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    ).json()["id"]
    collector_id = client.post(
        f"/v1/projects/{project_id}/collectors/register",
        headers=headers,
        json={"name": "collector", "environment": "test", "version": "0.1.0"},
    ).json()["collector_id"]
    sync_id = client.post(
        f"/v1/sources/{source_id}/syncs/start",
        headers=headers,
        json={"collector_id": collector_id, "diagnostics": {"total_files_seen": 1}},
    ).json()["sync_id"]

    first_content = "2026-05-05T10:00:00Z INFO api [deploy=abc1234] alphaunique timeout after 1500ms"
    first_hash = hashlib.sha256(first_content.encode("utf-8")).hexdigest()
    first_batch = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=headers,
        json={
            "sync_id": sync_id,
            "collector_id": collector_id,
            "documents": [
                {
                    "external_id": "logs/stream.log",
                    "path": "logs/stream.log",
                    "source_type": "logs",
                    "content": first_content,
                    "content_hash": first_hash,
                    "metadata": {"service_name": "api"},
                    "size_bytes": len(first_content),
                    "modified_at": "2026-05-05T10:00:00Z",
                }
            ],
        },
    )
    assert first_batch.status_code == 200
    assert first_batch.json()["created"] == 1
    first_chunk_count = asyncio.run(_document_chunk_count(project_id, source_id, "logs/stream.log"))
    assert first_chunk_count > 0

    second_batch = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=headers,
        json={
            "sync_id": sync_id,
            "collector_id": collector_id,
            "documents": [
                {
                    "external_id": "logs/stream.log",
                    "path": "logs/stream.log",
                    "source_type": "logs",
                    "content": first_content,
                    "content_hash": first_hash,
                    "metadata": {"service_name": "api"},
                    "size_bytes": len(first_content),
                    "modified_at": "2026-05-05T10:00:00Z",
                }
            ],
        },
    )
    assert second_batch.status_code == 200
    assert second_batch.json()["skipped_unchanged"] == 1
    assert asyncio.run(_document_chunk_count(project_id, source_id, "logs/stream.log")) == first_chunk_count

    updated_content = "2026-05-05T10:05:00Z INFO api [deploy=abc1234] betaunique timeout after 1700ms"
    updated_hash = hashlib.sha256(updated_content.encode("utf-8")).hexdigest()
    updated_batch = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=headers,
        json={
            "sync_id": sync_id,
            "collector_id": collector_id,
            "documents": [
                {
                    "external_id": "logs/stream.log",
                    "path": "logs/stream.log",
                    "source_type": "logs",
                    "content": updated_content,
                    "content_hash": updated_hash,
                    "metadata": {"service_name": "api"},
                    "size_bytes": len(updated_content),
                    "modified_at": "2026-05-05T10:05:00Z",
                }
            ],
        },
    )
    assert updated_batch.status_code == 200
    assert updated_batch.json()["updated"] == 1
    assert asyncio.run(_document_chunk_count(project_id, source_id, "logs/stream.log")) == first_chunk_count

    old_search = client.post(
        "/v1/search",
        headers=headers,
        json={"project_id": project_id, "query": "alphaunique", "top_k": 5},
    )
    new_search = client.post(
        "/v1/search",
        headers=headers,
        json={"project_id": project_id, "query": "betaunique", "top_k": 5},
    )
    assert old_search.status_code == 200
    assert new_search.status_code == 200
    old_previews = [item["text_preview"].lower() for item in old_search.json()["results"]]
    new_previews = [item["text_preview"].lower() for item in new_search.json()["results"]]
    assert all("alphaunique" not in preview for preview in old_previews)
    assert any("betaunique" in preview for preview in new_previews)


def test_batch_errors_do_not_fail_whole_request():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    project_id = _create_project(client, headers, "partial")
    source_id = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={"name": "mixed", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    ).json()["id"]
    collector_id = client.post(
        f"/v1/projects/{project_id}/collectors/register",
        headers=headers,
        json={"name": "collector", "environment": "partial", "version": "0.1.0"},
    ).json()["collector_id"]
    sync_id = client.post(
        f"/v1/sources/{source_id}/syncs/start",
        headers=headers,
        json={"collector_id": collector_id, "diagnostics": {"total_files_seen": 2}},
    ).json()["sync_id"]

    valid_content = "# Runbook\nInvestigate timeout by checking logs.\n"
    invalid_content = '{"bad": "json"}'
    response = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=headers,
        json={
            "sync_id": sync_id,
            "collector_id": collector_id,
            "documents": [
                {
                    "external_id": "docs/runbook.md",
                    "path": "docs/runbook.md",
                    "source_type": "runbook",
                    "content": valid_content,
                    "content_hash": hashlib.sha256(valid_content.encode("utf-8")).hexdigest(),
                    "metadata": {},
                    "size_bytes": len(valid_content),
                    "modified_at": "2026-05-05T10:00:00Z",
                },
                {
                    "external_id": "deploys/deploy-history.json",
                    "path": "deploys/deploy-history.json",
                    "source_type": "deploy",
                    "content": invalid_content,
                    "content_hash": hashlib.sha256(invalid_content.encode("utf-8")).hexdigest(),
                    "metadata": {},
                    "size_bytes": len(invalid_content),
                    "modified_at": "2026-05-05T10:00:00Z",
                },
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["received"] == 2
    assert payload["created"] == 1
    assert payload["skipped_invalid"] == 1
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["code"] == "parser_exception"
    assert payload["diagnostics"]["parser_error_reasons"]["parser_exception"] == 1
    assert payload["diagnostics"]["last_batch_parser_error_reasons"] == {"parser_exception": 1}

    finish = client.post(
        f"/v1/sources/{source_id}/syncs/{sync_id}/finish",
        headers=headers,
        json={"status": "success", "diagnostics": {"files_seen": 2, "files_skipped": 0, "parser_errors": 0}},
    )
    assert finish.status_code == 200
    assert finish.json()["status"] == "partial_success"
    repeated_finish = client.post(
        f"/v1/sources/{source_id}/syncs/{sync_id}/finish",
        headers=headers,
        json={"status": "success", "diagnostics": {"files_seen": 2, "files_skipped": 0, "parser_errors": 0}},
    )
    assert repeated_finish.status_code == 200
    assert repeated_finish.json()["status"] == "partial_success"

    latest = client.get(f"/v1/sources/{source_id}/syncs/latest", headers=headers)
    assert latest.status_code == 200
    latest_payload = latest.json()
    assert latest_payload["status"] == "partial_success"
    assert latest_payload["diagnostics"]["documents_created"] == 1
    assert latest_payload["diagnostics"]["skipped_invalid"] == 1
    assert latest_payload["coverage"]["has_runbooks"] is True
    assert latest_payload["coverage"]["has_incidents"] is False
    assert any("previous incidents" in warning.lower() for warning in latest_payload["coverage"]["warnings"])


def test_batch_validation_errors_are_isolated_and_do_not_echo_content():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    project_id = _create_project(client, headers, "validation")
    source_id = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={"name": "mixed", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    ).json()["id"]
    collector_id = client.post(
        f"/v1/projects/{project_id}/collectors/register",
        headers=headers,
        json={"name": "collector", "environment": "validation", "version": "0.1.0"},
    ).json()["collector_id"]
    sync_id = client.post(
        f"/v1/sources/{source_id}/syncs/start",
        headers=headers,
        json={"collector_id": collector_id, "diagnostics": {"total_files_seen": 2}},
    ).json()["sync_id"]

    valid_content = "2026-05-05T10:00:00Z WARN api [deploy=abc1234] gammaunique timeout"
    secret_content = "do not echo this content"
    response = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=headers,
        json={
            "sync_id": sync_id,
            "collector_id": collector_id,
            "documents": [
                {
                    "external_id": "logs/valid.log",
                    "path": "logs/valid.log",
                    "source_type": "logs",
                    "content": valid_content,
                    "content_hash": hashlib.sha256(valid_content.encode("utf-8")).hexdigest(),
                    "metadata": {"service_name": "api"},
                    "size_bytes": len(valid_content),
                    "modified_at": "2026-05-05T10:00:00Z",
                },
                {
                    "external_id": "../secret.log",
                    "path": "../secret.log",
                    "source_type": "logs",
                    "content": secret_content,
                    "content_hash": hashlib.sha256(secret_content.encode("utf-8")).hexdigest(),
                    "metadata": {},
                    "size_bytes": len(secret_content),
                    "modified_at": "2026-05-05T10:00:00Z",
                },
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["created"] == 1
    assert payload["skipped_invalid"] == 1
    assert payload["errors"][0]["code"] == "metadata_invalid"
    assert payload["diagnostics"]["parser_error_reasons"]["metadata_invalid"] == 1
    assert secret_content not in response.text

    search = client.post(
        "/v1/search",
        headers=headers,
        json={"project_id": project_id, "query": "gammaunique", "top_k": 3},
    )
    assert search.status_code == 200
    assert search.json()["total"] > 0


def test_source_delete_removes_chunks_from_search_and_reindex_refreshes_existing_chunks():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    project_id = _create_project(client, headers, "purge-source")
    source_id = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={"name": "logs", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    ).json()["id"]
    sync_id = client.post(f"/v1/sources/{source_id}/syncs/start", headers=headers, json={"diagnostics": {}}).json()[
        "sync_id"
    ]
    content = "2026-05-05T10:00:00Z ERROR api purgeunique timeout after 1500ms"
    batch = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=headers,
        json={
            "sync_id": sync_id,
            "documents": [
                {
                    "external_id": "logs/purge.log",
                    "path": "logs/purge.log",
                    "source_type": "logs",
                    "content": content,
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "metadata": {"service_name": "api"},
                    "size_bytes": len(content),
                    "modified_at": "2026-05-05T10:00:00Z",
                }
            ],
        },
    )
    assert batch.status_code == 200
    assert batch.json()["chunks_created"] > 0

    reindex = client.post(f"/v1/projects/{project_id}/sources/{source_id}/reindex", headers=headers)
    assert reindex.status_code == 200
    assert reindex.json()["chunks_reindexed"] == batch.json()["chunks_created"]
    assert reindex.json()["chunks_created"] == 0

    search = client.post(
        "/v1/search",
        headers=headers,
        json={"project_id": project_id, "query": "purgeunique", "top_k": 3},
    )
    assert search.status_code == 200
    assert search.json()["total"] > 0

    deleted = client.delete(f"/v1/projects/{project_id}/sources/{source_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["counts"]["chunks"] == batch.json()["chunks_created"]
    after = client.post(
        "/v1/search",
        headers=headers,
        json={"project_id": project_id, "query": "purgeunique", "top_k": 3},
    )
    assert after.status_code == 200
    assert after.json()["total"] == 0


def test_project_delete_cascades_safely():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    project_id = _create_project(client, headers, "purge-project")
    source_id = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={"name": "docs", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    ).json()["id"]
    sync_id = client.post(f"/v1/sources/{source_id}/syncs/start", headers=headers, json={"diagnostics": {}}).json()[
        "sync_id"
    ]
    content = "# Docs\nprojectdeleteunique runbook content"
    batch = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=headers,
        json={
            "sync_id": sync_id,
            "documents": [
                {
                    "external_id": "docs/delete.md",
                    "path": "docs/delete.md",
                    "source_type": "runbook",
                    "content": content,
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "metadata": {},
                    "size_bytes": len(content),
                    "modified_at": "2026-05-05T10:00:00Z",
                }
            ],
        },
    )
    assert batch.status_code == 200
    deleted = client.delete(f"/v1/projects/{project_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["counts"]["documents"] == 1
    assert deleted.json()["counts"]["chunks"] == batch.json()["chunks_created"]
    sources = client.get(f"/v1/projects/{project_id}/sources", headers=headers)
    assert sources.status_code == 404


def test_batch_limit_rejects_too_many_documents():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    project_id = _create_project(client, headers, "limit")
    source_id = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={"name": "logs", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    ).json()["id"]
    sync_id = client.post(f"/v1/sources/{source_id}/syncs/start", headers=headers, json={"diagnostics": {}}).json()[
        "sync_id"
    ]
    documents = []
    for index in range(101):
        content = f"2026-05-05T10:00:00Z INFO api line {index}"
        documents.append(
            {
                "external_id": f"logs/{index}.log",
                "path": f"logs/{index}.log",
                "source_type": "logs",
                "content": content,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "metadata": {},
                "size_bytes": len(content),
            }
        )
    response = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=headers,
        json={"sync_id": sync_id, "documents": documents},
    )
    assert response.status_code == 413


def test_partial_failure_audit_event_created():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    project_id = _create_project(client, headers, "audit")
    source_id = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={"name": "deploys", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    ).json()["id"]
    sync_id = client.post(f"/v1/sources/{source_id}/syncs/start", headers=headers, json={"diagnostics": {}}).json()[
        "sync_id"
    ]
    before = asyncio.run(_audit_count("documents_batch_partially_failed"))
    bad_content = "{not valid json"
    response = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=headers,
        json={
            "sync_id": sync_id,
            "documents": [
                {
                    "external_id": "deploys/deploy-history.json",
                    "path": "deploys/deploy-history.json",
                    "source_type": "deploy",
                    "content": bad_content,
                    "content_hash": hashlib.sha256(bad_content.encode("utf-8")).hexdigest(),
                    "metadata": {},
                    "size_bytes": len(bad_content),
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["skipped_invalid"] == 1
    after = asyncio.run(_audit_count("documents_batch_partially_failed"))
    assert after == before + 1


def test_wrong_project_collector_and_sync_source_mismatch_rejected():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    project_a = _create_project(client, headers, "scope-a")
    project_b = _create_project(client, headers, "scope-b")
    source_a = client.post(
        f"/v1/projects/{project_a}/sources",
        headers=headers,
        json={"name": "source-a", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    ).json()["id"]
    source_b = client.post(
        f"/v1/projects/{project_a}/sources",
        headers=headers,
        json={"name": "source-b", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    ).json()["id"]
    collector_b = client.post(
        f"/v1/projects/{project_b}/collectors/register",
        headers=headers,
        json={"name": "collector-b", "environment": "test", "version": "0.1.0"},
    ).json()["collector_id"]

    wrong_collector = client.post(
        f"/v1/sources/{source_a}/syncs/start",
        headers=headers,
        json={"collector_id": collector_b, "diagnostics": {}},
    )
    assert wrong_collector.status_code == 404

    sync_a = client.post(f"/v1/sources/{source_a}/syncs/start", headers=headers, json={"diagnostics": {}})
    assert sync_a.status_code == 200
    content = "2026-05-05T10:00:00Z INFO api source mismatch"
    mismatch = client.post(
        f"/v1/sources/{source_b}/documents/batch",
        headers=headers,
        json={
            "sync_id": sync_a.json()["sync_id"],
            "documents": [
                {
                    "external_id": "logs/mismatch.log",
                    "path": "logs/mismatch.log",
                    "source_type": "logs",
                    "content": content,
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "metadata": {},
                    "size_bytes": len(content),
                }
            ],
        },
    )
    assert mismatch.status_code == 404

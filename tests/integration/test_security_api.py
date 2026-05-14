from __future__ import annotations

import asyncio
import os
import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from incidentops.config.settings import get_settings
from incidentops.db.models import AuditEvent, ProjectMember, ProjectRole, User
from incidentops.security.passwords import hash_password

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")


def _login(client: httpx.Client, email: str = "admin@incidentops.local", password: str = "incidentops") -> dict[str, str]:
    response = client.post("/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    token = response.json()["access_token"]
    assert response.json()["token_type"] == "bearer"
    assert response.json()["expires_in"] > 0
    assert token.count(".") == 2
    return {"Authorization": f"Bearer {token}"}


def _create_project(client: httpx.Client, headers: dict[str, str]) -> str:
    response = client.post(
        "/v1/projects",
        headers=headers,
        json={"name": f"security-{os.urandom(4).hex()}", "demo_mode": False},
    )
    response.raise_for_status()
    return response.json()["project_id"]


async def _create_user_member(project_id: str, role: ProjectRole) -> tuple[str, str]:
    email = f"{role.value}-{uuid.uuid4().hex[:8]}@example.com"
    password = "correct-password"
    factory, engine = _test_session_factory()
    async with factory() as db:
        user = User(
            email=email,
            name=f"{role.value} user",
            password_hash=hash_password(password),
            password_scheme="bcrypt",
        )
        db.add(user)
        await db.flush()
        db.add(ProjectMember(project_id=uuid.UUID(project_id), user_id=user.id, role=role))
        await db.commit()
    await engine.dispose()
    return email, password


async def _audit_count(action: str) -> int:
    factory, engine = _test_session_factory()
    async with factory() as db:
        result = await db.execute(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == action))
        count = int(result.scalar_one())
    await engine.dispose()
    return count


def test_login_returns_jwt_and_auth_me_accepts_it():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    response = client.get("/v1/auth/me", headers=headers)
    assert response.status_code == 200
    assert response.json()["email"] == "admin@incidentops.local"


def test_invalid_token_rejected():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    response = client.get("/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_failed_and_successful_login_write_audit_events():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    before_failed = asyncio.run(_audit_count("login_failed"))
    failed = client.post("/v1/auth/login", json={"email": "admin@incidentops.local", "password": "wrong"})
    assert failed.status_code == 401
    after_failed = asyncio.run(_audit_count("login_failed"))
    assert after_failed == before_failed + 1

    before_success = asyncio.run(_audit_count("login_success"))
    _login(client)
    after_success = asyncio.run(_audit_count("login_success"))
    assert after_success == before_success + 1


def test_viewer_cannot_create_source_and_permission_denied_is_audited():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    admin_headers = _login(client)
    project_id = _create_project(client, admin_headers)
    viewer_email, viewer_password = asyncio.run(_create_user_member(project_id, ProjectRole.viewer))
    viewer_headers = _login(client, viewer_email, viewer_password)

    before = asyncio.run(_audit_count("permission_denied"))
    response = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=viewer_headers,
        json={"name": "logs", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    )
    assert response.status_code == 403
    after = asyncio.run(_audit_count("permission_denied"))
    assert after == before + 1


def test_viewer_cannot_batch_ingest():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    admin_headers = _login(client)
    project_id = _create_project(client, admin_headers)
    viewer_email, viewer_password = asyncio.run(_create_user_member(project_id, ProjectRole.viewer))
    viewer_headers = _login(client, viewer_email, viewer_password)

    source = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=admin_headers,
        json={"name": "logs", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    )
    assert source.status_code == 201
    source_id = source.json()["id"]
    sync = client.post(f"/v1/sources/{source_id}/syncs/start", headers=admin_headers, json={"diagnostics": {}})
    assert sync.status_code == 200

    denied = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=viewer_headers,
        json={"sync_id": sync.json()["sync_id"], "documents": []},
    )
    assert denied.status_code == 403


def test_batch_ingest_oversized_document_is_per_doc_error_and_audited():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    admin_headers = _login(client)
    project_id = _create_project(client, admin_headers)
    source = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=admin_headers,
        json={"name": "logs", "source_type": "filesystem", "sync_mode": "manual", "config": {}},
    )
    assert source.status_code == 201
    source_id = source.json()["id"]
    sync = client.post(f"/v1/sources/{source_id}/syncs/start", headers=admin_headers, json={"diagnostics": {}})
    assert sync.status_code == 200
    before = asyncio.run(_audit_count("documents_batch_ingested"))

    oversized = "x" * 2_000_001
    response = client.post(
        f"/v1/sources/{source_id}/documents/batch",
        headers=admin_headers,
        json={
            "sync_id": sync.json()["sync_id"],
            "documents": [
                {
                    "external_id": "logs/oversized.log",
                    "path": "logs/oversized.log",
                    "source_type": "logs",
                    "content": oversized,
                    "content_hash": "hash",
                    "metadata": {},
                    "size_bytes": len(oversized),
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["errors"][0]["error"] == "document exceeds configured byte limit"
    after = asyncio.run(_audit_count("documents_batch_ingested"))
    assert after == before + 1


def test_source_config_rejects_secret_values_without_echoing_secret():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    headers = _login(client)
    project_id = _create_project(client, headers)
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    response = client.post(
        f"/v1/projects/{project_id}/sources",
        headers=headers,
        json={
            "name": "logs",
            "source_type": "filesystem",
            "sync_mode": "manual",
            "config": {"description": secret},
        },
    )
    assert response.status_code == 400
    assert secret not in response.text


def test_investigator_cannot_approve_but_approver_can():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    admin_headers = _login(client)
    project_id = _create_project(client, admin_headers)
    run = client.post(
        "/v1/runs",
        headers=admin_headers,
        json={"project_id": project_id, "query": "What changed?", "top_k": 1, "create_issue_draft": False},
    )
    assert run.status_code == 200
    run_id = run.json()["run_id"]

    investigator_email, investigator_password = asyncio.run(_create_user_member(project_id, ProjectRole.investigator))
    investigator_headers = _login(client, investigator_email, investigator_password)
    denied = client.post(f"/v1/runs/{run_id}/approve", headers=investigator_headers, json={"rationale": "ok"})
    assert denied.status_code == 403

    approver_email, approver_password = asyncio.run(_create_user_member(project_id, ProjectRole.approver))
    approver_headers = _login(client, approver_email, approver_password)
    approved = client.post(f"/v1/runs/{run_id}/approve", headers=approver_headers, json={"rationale": "ok"})
    assert approved.status_code == 200


def test_non_member_cannot_search_project_and_limits_are_rejected():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    admin_headers = _login(client)
    project_id = _create_project(client, admin_headers)
    outsider_email, outsider_password = asyncio.run(_create_user_member(project_id, ProjectRole.viewer))
    outsider_headers = _login(client, outsider_email, outsider_password)
    asyncio.run(_remove_membership(project_id, outsider_email))

    forbidden = client.post(
        "/v1/search",
        headers=outsider_headers,
        json={"project_id": project_id, "query": "latency", "top_k": 1},
    )
    assert forbidden.status_code == 403

    too_long = client.post(
        "/v1/search",
        headers=admin_headers,
        json={"project_id": project_id, "query": "x" * 5000, "top_k": 1},
    )
    assert too_long.status_code == 422 or too_long.status_code == 400

    too_many = client.post(
        "/v1/search",
        headers=admin_headers,
        json={"project_id": project_id, "query": "latency", "top_k": 100},
    )
    assert too_many.status_code == 400


async def _remove_membership(project_id: str, email: str) -> None:
    factory, engine = _test_session_factory()
    async with factory() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one()
        result = await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == uuid.UUID(project_id),
                ProjectMember.user_id == user.id,
            )
        )
        member = result.scalar_one()
        await db.delete(member)
        await db.commit()
    await engine.dispose()


def _test_session_factory():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False), engine

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import check_rate_limit, ensure_project_access, get_current_user, get_db, get_settings_dep
from incidentops.config.settings import Settings
from incidentops.db.models import Project, ProjectMember, ProjectRole
from incidentops.ingestion.pipeline import run_ingestion
from incidentops.observability.metrics import incr
from incidentops.retrieval.embeddings import embed_texts
from incidentops.schemas.api import (
    CreateProjectRequest,
    CreateProjectResponse,
    IngestRequest,
    IngestResponse,
)
from incidentops.security.audit import record_audit_event

router = APIRouter(prefix="/v1", tags=["Projects & Ingestion"])


@router.post("/projects", response_model=CreateProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: CreateProjectRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    if settings.is_production_like and user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if settings.is_production_like and body.demo_mode:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="demo_mode projects are disabled in production")
    existing = await db.execute(select(Project).where(Project.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Project '{body.name}' already exists")
    project = Project(name=body.name, demo_mode=body.demo_mode)
    db.add(project)
    await db.flush()
    if user:
        db.add(ProjectMember(project_id=project.id, user_id=user.id, role=ProjectRole.admin))
        await db.flush()
    await record_audit_event(
        db,
        action="project_created",
        status="success",
        project_id=project.id,
        user=user,
        resource_type="project",
        resource_id=project.id,
        request=request,
        metadata={"demo_mode": body.demo_mode},
    )
    await db.commit()
    await db.refresh(project)
    return CreateProjectResponse(project_id=project.id, name=project.name, created_at=project.created_at.isoformat())


@router.post("/projects/{project_id}/ingest", response_model=IngestResponse)
async def ingest(
    project_id: uuid.UUID,
    body: IngestRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    if user:
        await check_rate_limit(
            db,
            settings,
            f"ingest:{user.id}",
            settings.project_ingestion_limit,
            3600,
            user=user,
            project_id=project_id,
            action="ingest",
        )
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.investigator)

    def embed_fn(texts: list[str]) -> list[list[float]]:
        return embed_texts(texts, model_name=settings.embedding_model)

    await record_audit_event(
        db,
        action="local_ingest_started",
        status="success",
        project_id=project_id,
        user=user,
        resource_type="project",
        resource_id=project_id,
        request=request,
        metadata={"mode": "local_path"},
    )
    stats = await run_ingestion(db, project_id, body.path, embed_fn=embed_fn)
    incr("documents_received_total", stats["documents_ingested"])
    incr("documents_indexed_total", stats["documents_ingested"])
    incr("chunks_created_total", stats["chunks_created"])
    incr("documents_skipped_total", stats["files_skipped"])
    incr("parser_errors_total", len(stats.get("parser_errors", [])))
    await record_audit_event(
        db,
        action="local_ingest_finished",
        status="success",
        project_id=project_id,
        user=user,
        resource_type="project",
        resource_id=project_id,
        request=request,
        metadata={
            "documents_ingested": stats["documents_ingested"],
            "chunks_created": stats["chunks_created"],
            "files_skipped": stats["files_skipped"],
        },
    )
    await record_audit_event(
        db,
        action="documents_batch_ingested",
        status="success",
        project_id=project_id,
        user=user,
        resource_type="project",
        resource_id=project_id,
        request=request,
        metadata={
            "mode": "local_path",
            "documents_ingested": stats["documents_ingested"],
            "chunks_created": stats["chunks_created"],
            "files_skipped": stats["files_skipped"],
        },
    )
    return IngestResponse(
        project_id=stats["project_id"],
        documents_ingested=stats["documents_ingested"],
        total_files_seen=stats["total_files_seen"],
        files_ingested=stats["files_ingested"],
        files_skipped=stats["files_skipped"],
        chunks_created=stats["chunks_created"],
        embedding_model=settings.embedding_model,
        duration_ms=stats["duration_ms"],
        skipped_files=stats.get("skipped_files", []),
        parser_errors=stats.get("parser_errors", []),
        source_type_counts=stats.get("source_type_counts", {}),
        chunk_type_counts=stats.get("chunk_type_counts", {}),
        source_coverage=stats["source_coverage"],
    )

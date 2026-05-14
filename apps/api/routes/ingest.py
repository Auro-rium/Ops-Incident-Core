from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import ensure_project_access, get_db, get_settings_dep, get_current_user
from incidentops.config.settings import Settings
from incidentops.db.models import Project, ProjectMember, ProjectRole
from incidentops.ingestion.pipeline import run_ingestion
from incidentops.retrieval.embeddings import embed_texts
from incidentops.schemas.api import (
    CreateProjectRequest,
    CreateProjectResponse,
    IngestRequest,
    IngestResponse,
)
from incidentops.security.rate_limit import limiter

router = APIRouter(prefix="/v1", tags=["Projects & Ingestion"])


@router.post("/projects", response_model=CreateProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: CreateProjectRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    existing = await db.execute(select(Project).where(Project.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Project '{body.name}' already exists")
    project = Project(name=body.name, demo_mode=body.demo_mode)
    db.add(project)
    await db.flush()
    if user:
        db.add(ProjectMember(project_id=project.id, user_id=user.id, role=ProjectRole.admin))
        await db.flush()
    await db.commit()
    await db.refresh(project)
    return CreateProjectResponse(project_id=project.id, name=project.name, created_at=project.created_at.isoformat())


@router.post("/projects/{project_id}/ingest", response_model=IngestResponse)
async def ingest(
    project_id: uuid.UUID,
    body: IngestRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    if user:
        limiter.check(f"ingest:{user.id}", settings.project_ingestion_limit, 3600)
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.investigator)

    def embed_fn(texts: list[str]) -> list[list[float]]:
        return embed_texts(texts, model_name=settings.embedding_model)

    stats = await run_ingestion(db, project_id, body.path, embed_fn=embed_fn)
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

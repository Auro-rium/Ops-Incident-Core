from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import check_rate_limit, get_current_user, get_db, get_settings_dep, require_user
from incidentops.config.settings import Settings
from incidentops.db.models import (
    AgentRun,
    AgentRunEvent,
    Approval,
    Chunk,
    Collector,
    Document,
    EvalRun,
    EvalRunCase,
    IncidentReportDraft,
    IssueDraft,
    Project,
    ProjectMember,
    ProjectRole,
    RetrievalResult,
    RetrievalRun,
    Source,
    SourceSync,
)
from incidentops.ingestion.pipeline import run_ingestion
from incidentops.observability.metrics import incr
from incidentops.retrieval.embeddings import embed_texts_async
from incidentops.schemas.api import (
    CreateProjectRequest,
    CreateProjectResponse,
    IngestRequest,
    IngestResponse,
    PurgeResponse,
)
from incidentops.security.audit import record_audit_event
from incidentops.security.path_policy import PathPolicyError, validate_path_under_allowed_roots
from incidentops.security.rbac import require_project_role

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


@router.delete("/projects/{project_id}", response_model=PurgeResponse)
async def delete_project(
    project_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    await require_project_role(db, project_id, user.id, ProjectRole.admin)
    counts = {
        "sources": await _count(db, select(func.count()).select_from(Source).where(Source.project_id == project_id)),
        "collectors": await _count(db, select(func.count()).select_from(Collector).where(Collector.project_id == project_id)),
        "syncs": await _count(db, select(func.count()).select_from(SourceSync).where(SourceSync.project_id == project_id)),
        "documents": await _count(db, select(func.count()).select_from(Document).where(Document.project_id == project_id)),
        "chunks": await _count(db, select(func.count()).select_from(Chunk).where(Chunk.project_id == project_id)),
        "retrieval_runs": await _count(
            db,
            select(func.count()).select_from(RetrievalRun).where(RetrievalRun.project_id == project_id),
        ),
        "agent_runs": await _count(db, select(func.count()).select_from(AgentRun).where(AgentRun.project_id == project_id)),
        "eval_runs": await _count(db, select(func.count()).select_from(EvalRun).where(EvalRun.project_id == project_id)),
        "memberships": await _count(
            db,
            select(func.count()).select_from(ProjectMember).where(ProjectMember.project_id == project_id),
        ),
    }
    agent_run_ids = select(AgentRun.id).where(AgentRun.project_id == project_id)
    eval_run_ids = select(EvalRun.id).where(EvalRun.project_id == project_id)
    retrieval_run_ids = select(RetrievalRun.id).where(RetrievalRun.project_id == project_id)

    await db.execute(delete(EvalRunCase).where(EvalRunCase.eval_run_id.in_(eval_run_ids)))
    await db.execute(delete(EvalRun).where(EvalRun.project_id == project_id))
    await db.execute(delete(IncidentReportDraft).where(IncidentReportDraft.run_id.in_(agent_run_ids)))
    await db.execute(delete(IssueDraft).where(IssueDraft.run_id.in_(agent_run_ids)))
    await db.execute(delete(Approval).where(Approval.run_id.in_(agent_run_ids)))
    await db.execute(delete(AgentRunEvent).where(AgentRunEvent.run_id.in_(agent_run_ids)))
    await db.execute(delete(AgentRun).where(AgentRun.project_id == project_id))
    await db.execute(delete(RetrievalResult).where(RetrievalResult.retrieval_run_id.in_(retrieval_run_ids)))
    await db.execute(delete(RetrievalRun).where(RetrievalRun.project_id == project_id))
    await db.execute(delete(Chunk).where(Chunk.project_id == project_id))
    await db.execute(delete(Document).where(Document.project_id == project_id))
    await db.execute(delete(SourceSync).where(SourceSync.project_id == project_id))
    await db.execute(delete(Source).where(Source.project_id == project_id))
    await db.execute(delete(Collector).where(Collector.project_id == project_id))
    await db.execute(delete(ProjectMember).where(ProjectMember.project_id == project_id))
    await record_audit_event(
        db,
        action="project_deleted",
        status="success",
        project_id=project_id,
        user=user,
        resource_type="project",
        resource_id=project_id,
        request=request,
        metadata={"project_id": str(project_id), "project_name": project.name, "delete_counts": counts},
    )
    await db.execute(delete(Project).where(Project.id == project_id))
    await db.commit()
    return PurgeResponse(deleted=True, resource_type="project", resource_id=project_id, counts=counts)


@router.post("/projects/{project_id}/ingest", response_model=IngestResponse)
async def ingest(
    project_id: uuid.UUID,
    body: IngestRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    if not settings.local_ingest_enabled or settings.is_production_like:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Local path ingest is disabled")
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
    await require_project_role(db, project_id, user.id, ProjectRole.admin)
    try:
        ingest_path = validate_path_under_allowed_roots(
            body.path,
            settings.local_ingest_allowed_roots,
            require_dir=True,
        )
    except PathPolicyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    async def embed_fn(texts: list[str]) -> list[list[float]]:
        return await embed_texts_async(texts, model_name=settings.embedding_model)

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
    stats = await run_ingestion(db, project_id, str(ingest_path), embed_fn=embed_fn)
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


async def _count(db: AsyncSession, statement) -> int:
    result = await db.execute(statement)
    return int(result.scalar_one() or 0)

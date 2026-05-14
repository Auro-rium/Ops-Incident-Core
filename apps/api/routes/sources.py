from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import ensure_project_access, get_current_user, get_db, get_settings_dep, require_user
from incidentops.config.settings import Settings
from incidentops.db.models import Collector, ProjectRole, Source, SourceSync
from incidentops.ingestion.indexer import index_normalized_documents
from incidentops.ingestion.normalized import NormalizedDocument
from incidentops.retrieval.embeddings import embed_texts
from incidentops.schemas.api import (
    BatchIngestErrorResponse,
    BatchIngestRequest,
    BatchIngestResponse,
    CollectorRegisterRequest,
    CollectorRegisterResponse,
    SourceCreateRequest,
    SourceResponse,
    SyncFinishRequest,
    SyncResponse,
    SyncStartRequest,
    SyncStatusResponse,
)
from incidentops.security.rbac import require_project_role

router = APIRouter(prefix="/v1", tags=["Sources"])

DISALLOWED_CONFIG_KEYS = {"password", "secret", "token", "credentials", "credential", "api_key", "apikey"}


@router.post("/projects/{project_id}/sources", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def create_source(
    project_id: uuid.UUID,
    body: SourceCreateRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.investigator)
    await require_project_role(db, project_id, user.id, ProjectRole.investigator)
    if _contains_disallowed_config_keys(body.config):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="config contains credential-like keys; use credentials_ref")
    source = Source(
        project_id=project_id,
        name=body.name,
        source_type=body.source_type,
        status="created",
        sync_mode=body.sync_mode,
        config_json=body.config,
    )
    db.add(source)
    await db.flush()
    await db.refresh(source)
    return _source_response(source)


@router.get("/projects/{project_id}/sources", response_model=list[SourceResponse])
async def list_sources(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.viewer)
    result = await db.execute(select(Source).where(Source.project_id == project_id).order_by(Source.created_at.asc()))
    return [_source_response(source) for source in result.scalars().all()]


@router.post("/projects/{project_id}/collectors/register", response_model=CollectorRegisterResponse)
async def register_collector(
    project_id: uuid.UUID,
    body: CollectorRegisterRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.investigator)
    await require_project_role(db, project_id, user.id, ProjectRole.investigator)
    now = _utcnow()
    statement = (
        insert(Collector)
        .values(
            project_id=project_id,
            name=body.name,
            environment=body.environment,
            version=body.version,
            status="active",
            last_seen_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_collectors_project_name_environment",
            set_={
                "version": body.version,
                "status": "active",
                "last_seen_at": now,
                "updated_at": now,
            },
        )
        .returning(Collector.id, Collector.status)
    )
    result = await db.execute(statement)
    collector_id, collector_status = result.one()
    return CollectorRegisterResponse(collector_id=collector_id, status=collector_status)


@router.post("/sources/{source_id}/syncs/start", response_model=SyncStatusResponse)
async def start_sync(
    source_id: uuid.UUID,
    body: SyncStartRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_user),
):
    source = await _require_source(db, source_id)
    await require_project_role(db, source.project_id, user.id, ProjectRole.investigator)
    collector = await _validate_collector(db, source.project_id, body.collector_id)
    sync = SourceSync(
        project_id=source.project_id,
        source_id=source.id,
        collector_id=collector.id if collector else None,
        status="running",
        started_at=_utcnow(),
        diagnostics_json=body.diagnostics or {},
        files_seen=int((body.diagnostics or {}).get("total_files_seen", 0) or 0),
    )
    db.add(sync)
    await db.flush()
    source.status = "syncing"
    source.last_sync_started_at = sync.started_at
    source.last_sync_status = "running"
    return SyncStatusResponse(sync_id=sync.id, status=sync.status)


@router.post("/sources/{source_id}/documents/batch", response_model=BatchIngestResponse)
async def ingest_documents_batch(
    source_id: uuid.UUID,
    body: BatchIngestRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    source = await _require_source(db, source_id)
    await require_project_role(db, source.project_id, user.id, ProjectRole.investigator)
    sync = await _require_sync(db, body.sync_id, source.id)
    await _validate_collector(db, source.project_id, body.collector_id)

    def embed_fn(texts: list[str]) -> list[list[float]]:
        return embed_texts(texts, model_name=settings.embedding_model)

    normalized_documents = [
        NormalizedDocument(
            external_id=document.external_id,
            path=document.path,
            source_type=document.source_type,
            content=document.content,
            content_hash=document.content_hash,
            metadata=document.metadata,
            size_bytes=document.size_bytes,
            modified_at=document.modified_at,
        )
        for document in body.documents
    ]
    result = await index_normalized_documents(
        db,
        source.project_id,
        source.id,
        sync.id,
        normalized_documents,
        embed_fn=embed_fn,
    )
    sync.documents_received += result.received
    sync.chunks_created += result.chunks_created
    sync.parser_errors += len(result.errors)
    diagnostics = dict(sync.diagnostics_json or {})
    diagnostics["last_batch_received"] = result.received
    diagnostics["last_batch_created"] = result.created
    diagnostics["last_batch_updated"] = result.updated
    diagnostics["last_batch_skipped_unchanged"] = result.skipped_unchanged
    sync.diagnostics_json = diagnostics
    await db.flush()
    return BatchIngestResponse(
        received=result.received,
        created=result.created,
        updated=result.updated,
        skipped_unchanged=result.skipped_unchanged,
        chunks_created=result.chunks_created,
        errors=[BatchIngestErrorResponse(**error.model_dump()) for error in result.errors],
    )


@router.post("/sources/{source_id}/syncs/{sync_id}/finish", response_model=SyncStatusResponse)
async def finish_sync(
    source_id: uuid.UUID,
    sync_id: uuid.UUID,
    body: SyncFinishRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_user),
):
    source = await _require_source(db, source_id)
    await require_project_role(db, source.project_id, user.id, ProjectRole.investigator)
    sync = await _require_sync(db, sync_id, source.id)
    sync.status = body.status
    sync.finished_at = _utcnow()
    diagnostics = body.diagnostics or {}
    coverage = body.coverage or {}
    sync.files_seen = int(diagnostics.get("files_seen", sync.files_seen) or 0)
    sync.files_skipped = int(diagnostics.get("files_skipped", sync.files_skipped) or 0)
    sync.parser_errors = int(diagnostics.get("parser_errors", sync.parser_errors) or 0)
    sync.coverage_json = coverage
    sync.diagnostics_json = diagnostics
    sync.error_message = diagnostics.get("error_message") or body.status if body.status != "success" else None

    source.status = "ready" if body.status == "success" else "error"
    source.last_sync_started_at = sync.started_at
    source.last_sync_finished_at = sync.finished_at
    source.last_sync_status = body.status
    source.last_error = sync.error_message
    await db.flush()
    return SyncStatusResponse(sync_id=sync.id, status=sync.status)


@router.get("/sources/{source_id}/syncs/latest", response_model=SyncResponse)
async def get_latest_sync(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    source = await _require_source(db, source_id)
    await ensure_project_access(db, source.project_id, user, settings, minimum_role=ProjectRole.viewer)
    result = await db.execute(
        select(SourceSync).where(SourceSync.source_id == source.id).order_by(SourceSync.started_at.desc()).limit(1)
    )
    sync = result.scalar_one_or_none()
    if not sync:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No sync found for source")
    return _sync_response(sync)


@router.get("/syncs/{sync_id}", response_model=SyncResponse)
async def get_sync(
    sync_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    result = await db.execute(select(SourceSync).where(SourceSync.id == sync_id))
    sync = result.scalar_one_or_none()
    if not sync:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Sync not found")
    await ensure_project_access(db, sync.project_id, user, settings, minimum_role=ProjectRole.viewer)
    return _sync_response(sync)


async def _require_source(db: AsyncSession, source_id: uuid.UUID) -> Source:
    result = await db.execute(select(Source).where(Source.id == source_id))
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source not found")
    return source


async def _require_sync(db: AsyncSession, sync_id: uuid.UUID, source_id: uuid.UUID) -> SourceSync:
    result = await db.execute(
        select(SourceSync).where(
            SourceSync.id == sync_id,
            SourceSync.source_id == source_id,
        )
    )
    sync = result.scalar_one_or_none()
    if not sync:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Sync not found for source")
    return sync


async def _validate_collector(db: AsyncSession, project_id: uuid.UUID, collector_id: uuid.UUID | None) -> Collector | None:
    if collector_id is None:
        return None
    result = await db.execute(
        select(Collector).where(
            Collector.id == collector_id,
            Collector.project_id == project_id,
        )
    )
    collector = result.scalar_one_or_none()
    if not collector:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Collector not found for project")
    collector.last_seen_at = _utcnow()
    return collector


def _contains_disallowed_config_keys(data: dict) -> bool:
    for key, value in data.items():
        normalized = key.lower().replace("-", "_")
        if normalized in DISALLOWED_CONFIG_KEYS:
            return True
        if any(token in normalized for token in ("password", "secret", "token", "credential", "api_key")):
            return True
        if isinstance(value, dict) and _contains_disallowed_config_keys(value):
            return True
    return False


def _source_response(source: Source) -> SourceResponse:
    return SourceResponse(
        id=source.id,
        project_id=source.project_id,
        name=source.name,
        source_type=source.source_type,
        status=source.status,
        sync_mode=source.sync_mode,
        last_sync_status=source.last_sync_status,
        last_sync_started_at=source.last_sync_started_at,
        last_sync_finished_at=source.last_sync_finished_at,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _sync_response(sync: SourceSync) -> SyncResponse:
    return SyncResponse(
        sync_id=sync.id,
        project_id=sync.project_id,
        source_id=sync.source_id,
        collector_id=sync.collector_id,
        status=sync.status,
        started_at=sync.started_at,
        finished_at=sync.finished_at,
        files_seen=sync.files_seen,
        documents_received=sync.documents_received,
        chunks_created=sync.chunks_created,
        files_skipped=sync.files_skipped,
        parser_errors=sync.parser_errors,
        coverage=sync.coverage_json or {},
        diagnostics=sync.diagnostics_json or {},
        error_message=sync.error_message,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

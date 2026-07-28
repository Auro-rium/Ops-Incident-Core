from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import (
    check_rate_limit,
    enforce_json_size,
    ensure_project_access,
    get_current_user,
    get_db,
    get_settings_dep,
    require_user,
)
from incidentops.config.settings import Settings
from incidentops.db.models import Chunk, Document, IndexJob, RetrievalResult, SourceSync, Collector, ProjectRole, Source
from incidentops.ingestion.diagnostics import build_source_coverage
from incidentops.ingestion.failure_taxonomy import failure_reason_counts
from incidentops.ingestion.indexer import index_normalized_documents
from incidentops.ingestion.normalized import (
    BatchIngestResult,
    DocumentBatchError,
    NormalizedDocument,
    safe_json_size,
    validate_normalized_document,
)
from incidentops.observability.metrics import incr
from incidentops.retrieval.embeddings import embed_texts_async
from incidentops.retrieval.vector_store import QdrantVectorStore, VectorStoreError
from incidentops.schemas.api import (
    BatchIngestErrorResponse,
    BatchIngestRequest,
    BatchIngestResponse,
    CollectorRegisterRequest,
    CollectorRegisterResponse,
    PurgeResponse,
    ReindexResponse,
    SourceCreateRequest,
    SourceResponse,
    SyncFinishRequest,
    SyncResponse,
    SyncStartRequest,
    SyncStatusResponse,
)
from incidentops.security.rbac import require_project_role
from incidentops.security.audit import record_audit_event
from incidentops.security.source_config import find_source_config_secret_violations

router = APIRouter(prefix="/v1", tags=["Sources"])

SYNC_STATUSES = {"running", "success", "partial_success", "failed", "cancelled"}


@router.post("/projects/{project_id}/sources", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def create_source(
    project_id: uuid.UUID,
    body: SourceCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.admin)
    await require_project_role(db, project_id, user.id, ProjectRole.admin)
    violations = find_source_config_secret_violations(body.config)
    if violations:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="config contains raw secret material; use credentials_ref")
    existing_result = await db.execute(
        select(Source)
        .where(
            Source.project_id == project_id,
            Source.name == body.name,
            Source.source_type == body.source_type,
        )
        .order_by(Source.created_at.asc())
        .limit(1)
    )
    existing_source = existing_result.scalar_one_or_none()
    if existing_source:
        return _source_response(existing_source)

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
    await record_audit_event(
        db,
        action="source_created",
        status="success",
        project_id=project_id,
        user=user,
        resource_type="source",
        resource_id=source.id,
        request=request,
        metadata={"source_type": body.source_type, "sync_mode": body.sync_mode},
    )
    await db.commit()
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


@router.delete("/projects/{project_id}/sources/{source_id}", response_model=PurgeResponse)
async def delete_source(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.admin)
    await require_project_role(db, project_id, user.id, ProjectRole.admin)
    source = await _require_source(db, source_id)
    if source.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source not found for project")

    counts = {
        "documents": await _count(db, select(func.count()).select_from(Document).where(Document.source_id == source_id)),
        "chunks": await _count(db, select(func.count()).select_from(Chunk).where(Chunk.source_id == source_id)),
        "syncs": await _count(db, select(func.count()).select_from(SourceSync).where(SourceSync.source_id == source_id)),
        "retrieval_results": await _count_source_retrieval_results(db, source_id),
    }
    try:
        await QdrantVectorStore(settings).delete_by_filter(project_id, source_id=str(source_id))
    except VectorStoreError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Vector store deletion failed") from exc
    chunk_ids = select(Chunk.id).where(Chunk.source_id == source_id)
    await db.execute(delete(RetrievalResult).where(RetrievalResult.chunk_id.in_(chunk_ids)))
    await db.execute(delete(Chunk).where(Chunk.source_id == source_id))
    await db.execute(delete(Document).where(Document.source_id == source_id))
    await db.execute(delete(SourceSync).where(SourceSync.source_id == source_id))
    await db.execute(delete(Source).where(Source.id == source_id, Source.project_id == project_id))
    await record_audit_event(
        db,
        action="source_deleted",
        status="success",
        project_id=project_id,
        user=user,
        resource_type="source",
        resource_id=source_id,
        request=request,
        metadata={"source_id": str(source_id), "source_name": source.name, "delete_counts": counts},
    )
    await db.commit()
    return PurgeResponse(deleted=True, resource_type="source", resource_id=source_id, counts=counts)


@router.post("/projects/{project_id}/sources/{source_id}/reindex", response_model=ReindexResponse)
async def reindex_source(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.admin)
    await require_project_role(db, project_id, user.id, ProjectRole.admin)
    source = await _require_source(db, source_id)
    if source.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source not found for project")

    documents_reindexed = await _count(
        db,
        select(func.count()).select_from(Document).where(Document.project_id == project_id, Document.source_id == source_id),
    )
    chunks_reindexed = await _count(
        db,
        select(func.count()).select_from(Chunk).where(Chunk.project_id == project_id, Chunk.source_id == source_id),
    )
    await db.execute(
        update(Chunk)
        .where(Chunk.project_id == project_id, Chunk.source_id == source_id)
        .values(search_tsvector=func.to_tsvector("english", Chunk.text))
    )
    source_type_counts = await _source_type_counts(db, project_id, source_id)
    chunk_type_counts = await _chunk_type_counts(db, project_id, source_id)
    diagnostics = {
        "reindex_mode": "refresh_existing_chunks",
        "documents_reindexed": documents_reindexed,
        "chunks_reindexed": chunks_reindexed,
        "chunks_created": 0,
        "source_type_counts": source_type_counts,
        "chunk_type_counts": chunk_type_counts,
        "reindexed_at": _utcnow().isoformat(),
    }
    latest = await _latest_sync_for_source(db, source_id)
    if latest:
        merged = dict(latest.diagnostics_json or {})
        merged.update({"last_reindex": diagnostics})
        latest.diagnostics_json = merged
        latest.coverage_json = build_source_coverage(source_type_counts, chunk_type_counts)
    await record_audit_event(
        db,
        action="source_reindexed",
        status="success",
        project_id=project_id,
        user=user,
        resource_type="source",
        resource_id=source_id,
        request=request,
        metadata={
            "source_id": str(source_id),
            "documents_reindexed": documents_reindexed,
            "chunks_reindexed": chunks_reindexed,
            "chunks_created": 0,
        },
    )
    await db.commit()
    return ReindexResponse(
        source_id=source_id,
        documents_reindexed=documents_reindexed,
        chunks_reindexed=chunks_reindexed,
        chunks_created=0,
        diagnostics=diagnostics,
    )


@router.post("/projects/{project_id}/collectors/register", response_model=CollectorRegisterResponse)
async def register_collector(
    project_id: uuid.UUID,
    body: CollectorRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.admin)
    await require_project_role(db, project_id, user.id, ProjectRole.admin)
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
    await record_audit_event(
        db,
        action="collector_registered",
        status="success",
        project_id=project_id,
        user=user,
        resource_type="collector",
        resource_id=collector_id,
        request=request,
        metadata={"name": body.name, "environment": body.environment, "version": body.version},
    )
    await db.commit()
    return CollectorRegisterResponse(collector_id=collector_id, status=collector_status)


@router.post("/sources/{source_id}/syncs/start", response_model=SyncStatusResponse)
async def start_sync(
    source_id: uuid.UUID,
    body: SyncStartRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    source = await _require_source(db, source_id)
    await require_project_role(db, source.project_id, user.id, ProjectRole.admin)
    enforce_json_size(body.diagnostics, settings.max_sync_diagnostics_bytes, "sync diagnostics")
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
    source.last_error = None
    await record_audit_event(
        db,
        action="sync_started",
        status="success",
        project_id=source.project_id,
        user=user,
        resource_type="source_sync",
        resource_id=sync.id,
        request=request,
        metadata={"source_id": str(source.id), "collector_id": str(collector.id) if collector else None},
    )
    await db.commit()
    return SyncStatusResponse(sync_id=sync.id, status=sync.status)


@router.post("/sources/{source_id}/documents/batch", response_model=BatchIngestResponse)
async def ingest_documents_batch(
    source_id: uuid.UUID,
    body: BatchIngestRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    source = await _require_source(db, source_id)
    await require_project_role(db, source.project_id, user.id, ProjectRole.admin)
    await check_rate_limit(
        db,
        settings,
        f"batch_ingest:{user.id}",
        settings.collector_batch_request_limit,
        3600,
        user=user,
        project_id=source.project_id,
        action="documents_batch_ingest",
    )
    sync = await _require_sync(db, body.sync_id, source.id)
    await _validate_collector(db, source.project_id, body.collector_id)
    _validate_sync_collector(sync, body.collector_id)
    if sync.collector_id is None and body.collector_id is not None:
        sync.collector_id = body.collector_id
    if len(body.documents) > settings.max_documents_per_batch:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="document batch exceeds configured count limit")
    try:
        batch_bytes = safe_json_size(body.model_dump(mode="json"))
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="document batch must be JSON-serializable")
    if batch_bytes > settings.max_batch_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="document batch exceeds configured byte limit")

    async def embed_fn(texts: list[str]) -> list[list[float]]:
        return await embed_texts_async(texts, model_name=settings.embedding_model)

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
    queued_index_jobs = 0
    if settings.rag_async_indexing:
        validated_documents: list[NormalizedDocument] = []
        validation_errors: list[DocumentBatchError] = []
        seen_external_ids: set[str] = set()
        for document in normalized_documents:
            validation = validate_normalized_document(document, settings)
            if isinstance(validation, DocumentBatchError):
                validation_errors.append(validation)
                continue
            if validation.external_id in seen_external_ids:
                validation_errors.append(
                    DocumentBatchError(
                        external_id=validation.external_id,
                        path=validation.path,
                        code="duplicate_external_id",
                        error="duplicate external_id in batch",
                        message="duplicate external_id in batch",
                    )
                )
                continue
            seen_external_ids.add(validation.external_id)
            validated_documents.append(validation)
        index_jobs = [
            IndexJob(
                project_id=source.project_id,
                source_id=source.id,
                sync_id=sync.id,
                normalized_payload_json=document.model_dump(mode="json"),
                content_hash=document.content_hash,
                index_version=settings.vector_index_version,
            )
            for document in validated_documents
        ]
        db.add_all(index_jobs)
        await db.flush()
        queued_index_jobs = len(index_jobs)
        result = BatchIngestResult(
            received=len(normalized_documents),
            skipped_invalid=len(validation_errors),
            errors=validation_errors,
            embedding_backend=settings.embedding_model,
            diagnostics={"index_jobs_queued": queued_index_jobs},
        )
    else:
        result = await index_normalized_documents(
            db,
            source.project_id,
            source.id,
            sync.id,
            normalized_documents,
            embed_fn=embed_fn,
        )
    all_errors = [BatchIngestErrorResponse(**error.model_dump()) for error in result.errors]
    sync.documents_received += len(body.documents)
    sync.chunks_created += result.chunks_created
    sync.parser_errors += len(all_errors)
    diagnostics = _merge_batch_diagnostics(
        dict(sync.diagnostics_json or {}),
        result,
        len(all_errors),
        {
            "collector_version": body.collector_version,
            "schema_version": body.schema_version,
            "core_api_version": body.core_api_version,
        },
    )
    diagnostics["index_jobs_queued"] = int(diagnostics.get("index_jobs_queued", 0) or 0) + queued_index_jobs
    sync.diagnostics_json = diagnostics
    sync.coverage_json = build_source_coverage(
        diagnostics.get("source_type_counts", {}),
        diagnostics.get("chunk_type_counts", {}),
    )
    if all_errors and sync.status == "running":
        source.last_sync_status = "running_with_errors"
    incr("documents_received_total", len(body.documents))
    incr("documents_indexed_total", result.created + result.updated)
    incr("chunks_created_total", result.chunks_created)
    incr("documents_skipped_total", result.skipped_unchanged + result.skipped_invalid)
    incr("parser_errors_total", len(all_errors))
    await record_audit_event(
        db,
        action="documents_batch_ingested",
        status="success" if not all_errors else "partial",
        project_id=source.project_id,
        user=user,
        resource_type="source",
        resource_id=source.id,
        request=request,
        metadata={
            "sync_id": str(sync.id),
            "received": len(body.documents),
            "created": result.created,
            "updated": result.updated,
            "skipped_unchanged": result.skipped_unchanged,
            "skipped_invalid": result.skipped_invalid,
            "chunks_created": result.chunks_created,
            "error_count": len(all_errors),
            "index_jobs_queued": queued_index_jobs,
        },
    )
    if all_errors:
        await record_audit_event(
            db,
            action="documents_batch_partially_failed",
            status="partial",
            project_id=source.project_id,
            user=user,
            resource_type="source",
            resource_id=source.id,
            request=request,
            metadata={
                "sync_id": str(sync.id),
                "received": len(body.documents),
                "created": result.created,
                "updated": result.updated,
                "skipped_unchanged": result.skipped_unchanged,
                "skipped_invalid": result.skipped_invalid,
                "chunks_created": result.chunks_created,
                "error_count": len(all_errors),
            },
        )
    await db.flush()
    await db.commit()
    return BatchIngestResponse(
        received=len(body.documents),
        created=result.created,
        updated=result.updated,
        skipped_unchanged=result.skipped_unchanged,
        skipped_invalid=result.skipped_invalid,
        chunks_created=result.chunks_created,
        errors=all_errors,
        diagnostics=diagnostics,
        coverage=sync.coverage_json or result.coverage,
        embedding_backend=result.embedding_backend,
        warnings=result.warnings,
    )


@router.post("/sources/{source_id}/syncs/{sync_id}/finish", response_model=SyncStatusResponse)
async def finish_sync(
    source_id: uuid.UUID,
    sync_id: uuid.UUID,
    body: SyncFinishRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    source = await _require_source(db, source_id)
    await require_project_role(db, source.project_id, user.id, ProjectRole.admin)
    enforce_json_size(body.diagnostics, settings.max_sync_diagnostics_bytes, "sync diagnostics")
    enforce_json_size(body.coverage, settings.max_sync_diagnostics_bytes, "sync coverage")
    sync = await _require_sync(db, sync_id, source.id)
    if body.status not in SYNC_STATUSES - {"running"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid sync status")
    if sync.finished_at is not None:
        if sync.status == body.status or (sync.status == "partial_success" and body.status == "success"):
            return SyncStatusResponse(sync_id=sync.id, status=sync.status)
        raise HTTPException(status.HTTP_409_CONFLICT, detail="sync is already finished")
    diagnostics = {**(sync.diagnostics_json or {}), **(body.diagnostics or {})}
    parser_errors = max(
        sync.parser_errors,
        int(diagnostics.get("parser_errors", sync.parser_errors) or 0),
        int(diagnostics.get("parser_error_count", 0) or 0),
    )
    error_count = int(diagnostics.get("error_count", 0) or 0)
    effective_status = (
        "partial_success"
        if body.status == "success" and (sync.parser_errors > 0 or parser_errors > 0 or error_count > 0)
        else body.status
    )
    sync.status = effective_status
    sync.finished_at = _utcnow()
    coverage = _normalize_coverage(body.coverage or sync.coverage_json or {}, diagnostics)
    sync.files_seen = int(diagnostics.get("files_seen", sync.files_seen) or 0)
    sync.files_skipped = int(diagnostics.get("files_skipped", sync.files_skipped) or 0)
    sync.parser_errors = parser_errors
    sync.coverage_json = coverage
    sync.diagnostics_json = diagnostics
    sync.error_message = diagnostics.get("error_message") or (effective_status if effective_status in {"failed", "cancelled"} else None)

    pending_index_jobs = 0
    if settings.rag_async_indexing:
        pending_index_jobs = await _count(
            db,
            select(func.count())
            .select_from(IndexJob)
            .where(IndexJob.sync_id == sync.id, IndexJob.status.in_(("pending", "queued", "running"))),
        )
        diagnostics["index_jobs_pending"] = pending_index_jobs
    source.status = (
        "indexing"
        if pending_index_jobs and effective_status in {"success", "partial_success"}
        else "ready" if effective_status in {"success", "partial_success"} else "error"
    )
    source.last_sync_started_at = sync.started_at
    source.last_sync_finished_at = sync.finished_at
    source.last_sync_status = "indexing" if pending_index_jobs else effective_status
    source.last_error = sync.error_message
    await record_audit_event(
        db,
        action="sync_failed" if effective_status == "failed" else "sync_finished",
        status=effective_status,
        project_id=source.project_id,
        user=user,
        resource_type="source_sync",
        resource_id=sync.id,
        request=request,
        metadata={
            "source_id": str(source.id),
            "files_seen": sync.files_seen,
            "files_skipped": sync.files_skipped,
            "parser_errors": sync.parser_errors,
        },
    )
    await db.flush()
    await db.commit()
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


def _validate_sync_collector(sync: SourceSync, collector_id: uuid.UUID | None) -> None:
    if sync.collector_id is None or collector_id is None:
        return
    if sync.collector_id != collector_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="collector does not match sync")


def _merge_batch_diagnostics(
    diagnostics: dict,
    result,
    error_count: int,
    version_metadata: dict[str, str | None] | None = None,
) -> dict:
    for key, value in (version_metadata or {}).items():
        if value is not None:
            diagnostics[key] = value
    diagnostics["last_batch_received"] = result.received
    diagnostics["last_batch_created"] = result.created
    diagnostics["last_batch_updated"] = result.updated
    diagnostics["last_batch_skipped_unchanged"] = result.skipped_unchanged
    diagnostics["last_batch_skipped_invalid"] = result.skipped_invalid
    diagnostics["last_batch_chunks_created"] = result.chunks_created
    diagnostics["last_batch_error_count"] = error_count
    diagnostics["documents_created"] = int(diagnostics.get("documents_created", 0) or 0) + result.created
    diagnostics["documents_updated"] = int(diagnostics.get("documents_updated", 0) or 0) + result.updated
    diagnostics["skipped_unchanged"] = int(diagnostics.get("skipped_unchanged", 0) or 0) + result.skipped_unchanged
    diagnostics["skipped_invalid"] = int(diagnostics.get("skipped_invalid", 0) or 0) + result.skipped_invalid
    diagnostics["error_count"] = int(diagnostics.get("error_count", 0) or 0) + error_count
    parser_error_reasons = result.diagnostics.get("parser_error_reasons") or failure_reason_counts(
        error.code for error in result.errors
    )
    diagnostics["parser_error_reasons"] = _merge_counts(
        diagnostics.get("parser_error_reasons", {}),
        parser_error_reasons,
    )
    diagnostics["parser_error_count"] = int(diagnostics.get("parser_error_count", 0) or 0) + int(
        result.diagnostics.get("parser_error_count", error_count) or 0
    )
    diagnostics["chunk_discard_reasons"] = _merge_counts(
        diagnostics.get("chunk_discard_reasons", {}),
        result.diagnostics.get("chunk_discard_reasons", {}),
    )
    diagnostics["embedding_failures"] = int(diagnostics.get("embedding_failures", 0) or 0) + int(
        result.diagnostics.get("embedding_failures", 0) or 0
    )
    diagnostics["source_type_counts"] = _merge_counts(
        diagnostics.get("source_type_counts", {}),
        result.source_type_counts,
    )
    diagnostics["chunk_type_counts"] = _merge_counts(
        diagnostics.get("chunk_type_counts", {}),
        result.chunk_type_counts,
    )
    diagnostics["warnings"] = result.warnings
    diagnostics["embedding_backend"] = result.embedding_backend
    return diagnostics


def _normalize_coverage(coverage: dict, diagnostics: dict) -> dict:
    source_type_counts = coverage.get("source_type_counts") or diagnostics.get("source_type_counts", {})
    chunk_type_counts = coverage.get("chunk_type_counts") or diagnostics.get("chunk_type_counts", {})
    computed = build_source_coverage(source_type_counts, chunk_type_counts)
    return {**computed, **coverage, "source_type_counts": source_type_counts, "chunk_type_counts": chunk_type_counts}


def _merge_counts(left: dict, right: dict) -> dict:
    merged = dict(left or {})
    for key, value in (right or {}).items():
        merged[key] = int(merged.get(key, 0) or 0) + int(value or 0)
    return merged


async def _count(db: AsyncSession, statement) -> int:
    result = await db.execute(statement)
    return int(result.scalar_one() or 0)


async def _count_source_retrieval_results(db: AsyncSession, source_id: uuid.UUID) -> int:
    chunk_ids = select(Chunk.id).where(Chunk.source_id == source_id)
    return await _count(
        db,
        select(func.count()).select_from(RetrievalResult).where(RetrievalResult.chunk_id.in_(chunk_ids)),
    )


async def _source_type_counts(db: AsyncSession, project_id: uuid.UUID, source_id: uuid.UUID) -> dict[str, int]:
    result = await db.execute(
        select(Document.source_type, func.count())
        .where(Document.project_id == project_id, Document.source_id == source_id)
        .group_by(Document.source_type)
    )
    return {str(source_type or "unknown_text"): int(count or 0) for source_type, count in result.all()}


async def _chunk_type_counts(db: AsyncSession, project_id: uuid.UUID, source_id: uuid.UUID) -> dict[str, int]:
    result = await db.execute(
        select(Chunk.chunk_type, func.count())
        .where(Chunk.project_id == project_id, Chunk.source_id == source_id)
        .group_by(Chunk.chunk_type)
    )
    return {str(chunk_type or "unknown"): int(count or 0) for chunk_type, count in result.all()}


async def _latest_sync_for_source(db: AsyncSession, source_id: uuid.UUID) -> SourceSync | None:
    result = await db.execute(
        select(SourceSync).where(SourceSync.source_id == source_id).order_by(SourceSync.started_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


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

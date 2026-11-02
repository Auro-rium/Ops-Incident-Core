from __future__ import annotations

import logging
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
from incidentops.ingestion.failure_taxonomy import (
    FAILURE_BINARY,
    FAILURE_EMPTY,
    FAILURE_GENERATED_FILE,
    FAILURE_OVERSIZED,
    FAILURE_UNSUPPORTED_EXTENSION,
    FAILURE_VENDOR_FILE,
    failure_reason_counts,
    normalize_failure_code,
)
from incidentops.ingestion.indexer import (
    INDEX_REASON_KEY,
    INDEX_REASON_REINDEX,
    delete_document_vectors,
    index_normalized_documents,
    prune_stale_documents,
)
from incidentops.ingestion.normalized import (
    BatchIngestResult,
    DocumentBatchError,
    NormalizedDocument,
    safe_json_size,
    validate_normalized_document,
)
from incidentops.observability.metrics import incr
from incidentops.operations.service import record_operational_event
from incidentops.retrieval.embeddings import embed_texts_async
from incidentops.retrieval.cache import invalidate_project
from incidentops.retrieval.vector_store import QdrantVectorStore, VectorStoreError, chunk_point
from incidentops.schemas.api import (
    BatchIngestErrorResponse,
    BatchIngestRequest,
    BatchIngestResponse,
    CollectorRegisterRequest,
    CollectorRegisterResponse,
    PurgeResponse,
    ReindexResponse,
    SourceIntegrityResponse,
    SourceCreateRequest,
    SourceResponse,
    SyncFinishRequest,
    SyncResponse,
    SyncStartRequest,
    SyncStatusResponse,
)
from incidentops.security.audit import record_audit_event
from incidentops.security.rbac import get_project_role
from incidentops.security.source_config import find_source_config_secret_violations

logger = logging.getLogger("incidentops.api.sources")

router = APIRouter(prefix="/v1", tags=["Sources"])

SYNC_STATUSES = {"running", "success", "partial_success", "failed", "cancelled"}
ACTIVE_INDEX_JOB_STATUSES = {"pending", "queued", "running"}
# Collector skips that say a file should not be indexed. Any other reason means
# it could not be read, so its absence from the sync does not mean it was deleted.
POLICY_SKIP_REASONS = {
    "unchanged",
    FAILURE_BINARY,
    FAILURE_EMPTY,
    FAILURE_GENERATED_FILE,
    FAILURE_OVERSIZED,
    FAILURE_UNSUPPORTED_EXTENSION,
    FAILURE_VENDOR_FILE,
}
_REINDEX_PAGE_SIZE = 128
_SEEN_MARK_BATCH_SIZE = 500


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
        await QdrantVectorStore(settings).delete_by_filter(
            project_id, all_index_versions=True, source_id=str(source_id)
        )
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
    await invalidate_project(project_id)
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
    # Re-embedding calls the configured (possibly paid) embedding provider.
    chunks_created = 0
    job_counts: dict[str, int] = {}
    if settings.rag_async_indexing:
        reindex_mode = "index_jobs"
        job_counts = await _queue_reindex_jobs(db, source, settings)
    else:
        reindex_mode = "reembed_inline"
        chunks_created = await _reembed_source_chunks(db, project_id, source_id, settings)
    # Last, so these row locks are not held during the embedding calls above.
    await db.execute(
        update(Chunk)
        .where(Chunk.project_id == project_id, Chunk.source_id == source_id)
        .values(search_tsvector=func.to_tsvector("english", Chunk.text))
    )
    source_type_counts = await _source_type_counts(db, project_id, source_id)
    chunk_type_counts = await _chunk_type_counts(db, project_id, source_id)
    diagnostics = {
        "reindex_mode": reindex_mode,
        "documents_reindexed": documents_reindexed,
        "chunks_reindexed": chunks_reindexed,
        "chunks_created": chunks_created,
        **job_counts,
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
            "chunks_created": chunks_created,
            **job_counts,
        },
    )
    await record_operational_event(
        db,
        project_id=project_id,
        category="indexing",
        event_type="source_reindexed",
        payload={
            "source_id": str(source_id),
            "reindex_mode": reindex_mode,
            "documents_reindexed": documents_reindexed,
            "chunks_reindexed": chunks_reindexed,
            "chunks_created": chunks_created,
            **job_counts,
        },
    )
    await db.commit()
    await invalidate_project(project_id)
    return ReindexResponse(
        source_id=source_id,
        documents_reindexed=documents_reindexed,
        chunks_reindexed=chunks_reindexed,
        chunks_created=chunks_created,
        diagnostics=diagnostics,
    )


@router.get("/projects/{project_id}/sources/{source_id}/integrity", response_model=SourceIntegrityResponse)
async def source_integrity(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    """Expose bounded source integrity counters without exposing evidence text."""
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.viewer)
    source = await _require_source(db, source_id)
    if source.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source not found for project")
    document_count = await _count(
        db, select(func.count()).select_from(Document).where(Document.project_id == project_id, Document.source_id == source_id)
    )
    chunk_count = await _count(
        db, select(func.count()).select_from(Chunk).where(Chunk.project_id == project_id, Chunk.source_id == source_id)
    )
    duplicate_groups = (
        select(
            Chunk.document_id,
            Chunk.chunk_type,
            Chunk.start_line,
            Chunk.end_line,
            func.md5(Chunk.text).label("text_digest"),
            func.count().label("row_count"),
        )
        .where(Chunk.project_id == project_id, Chunk.source_id == source_id)
        .group_by(Chunk.document_id, Chunk.chunk_type, Chunk.start_line, Chunk.end_line, func.md5(Chunk.text))
        .having(func.count() > 1)
        .subquery()
    )
    duplicate_chunk_groups = await _count(db, select(func.count()).select_from(duplicate_groups))
    duplicate_chunk_rows = await _count(db, select(func.coalesce(func.sum(duplicate_groups.c.row_count), 0)))
    return SourceIntegrityResponse(
        source_id=source_id,
        document_count=document_count,
        chunk_count=chunk_count,
        duplicate_chunk_groups=duplicate_chunk_groups,
        duplicate_chunk_rows=duplicate_chunk_rows,
        active_index_version=settings.vector_index_version,
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
    source = await _require_accessible_source(db, source_id, user, settings, ProjectRole.admin)
    enforce_json_size(body.diagnostics, settings.max_sync_diagnostics_bytes, "sync diagnostics")
    collector = await _validate_collector(db, source.project_id, body.collector_id)
    # Overlapping syncs overwrite each other's last_seen_sync_id, and whichever
    # finished first would prune what the other saw. Only the newest sync lives.
    await db.execute(
        update(SourceSync)
        .where(SourceSync.source_id == source.id, SourceSync.status == "running")
        .values(status="cancelled", finished_at=_utcnow(), error_message="superseded by a newer sync")
    )
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
    source = await _require_accessible_source(db, source_id, user, settings, ProjectRole.admin)
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
    # Index jobs update this row concurrently in async mode. Inline mode commits
    # mid-request (and has no such writers), so a lock would not survive anyway.
    sync = await _require_sync(db, body.sync_id, source.id, settings.rag_async_indexing)
    if sync.status != "running":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="sync is not running")
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
    # Everything received in this sync (valid or not) is protected from pruning.
    await _mark_documents_seen(db, source, sync.id, [document.external_id for document in body.documents])
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
                        code=normalize_failure_code("duplicate_external_id"),
                        error="duplicate external_id in batch",
                        message="duplicate external_id in batch",
                    )
                )
                continue
            seen_external_ids.add(validation.external_id)
            validated_documents.append(validation)
        existing_hashes = await _existing_document_hashes(
            db, source, [document.external_id for document in validated_documents]
        )
        index_jobs: list[IndexJob] = []
        created = updated = skipped_unchanged = 0
        for document in validated_documents:
            if document.external_id in existing_hashes:
                if existing_hashes[document.external_id] == document.content_hash:
                    skipped_unchanged += 1
                    continue
                updated += 1
            else:
                created += 1
            index_jobs.append(
                IndexJob(
                    project_id=source.project_id,
                    source_id=source.id,
                    sync_id=sync.id,
                    normalized_payload_json=document.model_dump(mode="json"),
                    content_hash=document.content_hash,
                    index_version=settings.vector_index_version,
                )
            )
        db.add_all(index_jobs)
        await db.flush()
        queued_index_jobs = len(index_jobs)
        result = BatchIngestResult(
            received=len(normalized_documents),
            created=created,
            updated=updated,
            skipped_unchanged=skipped_unchanged,
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
    await record_operational_event(
        db,
        project_id=source.project_id,
        category="ingestion",
        event_type="documents_batch_completed",
        severity="medium" if all_errors else "info",
        payload={
            "source_id": str(source.id),
            "sync_id": str(sync.id),
            "received": len(body.documents),
            "created": result.created,
            "updated": result.updated,
            "skipped_unchanged": result.skipped_unchanged,
            "skipped_invalid": result.skipped_invalid,
            "chunks_created": result.chunks_created,
            "parser_error_count": len(all_errors),
            "embedding_failures": int(
                (result.diagnostics or {}).get("embedding_failures", 0) or 0
            ),
            "async_indexing": settings.rag_async_indexing,
            "index_jobs_queued": queued_index_jobs,
        },
    )
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
    source = await _require_accessible_source(db, source_id, user, settings, ProjectRole.admin)
    enforce_json_size(body.diagnostics, settings.max_sync_diagnostics_bytes, "sync diagnostics")
    enforce_json_size(body.coverage, settings.max_sync_diagnostics_bytes, "sync coverage")
    sync = await _require_sync(db, sync_id, source.id, True)
    if body.status not in SYNC_STATUSES - {"running"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid sync status")
    if sync.finished_at is not None:
        if sync.status == body.status or (sync.status == "partial_success" and body.status == "success"):
            return SyncStatusResponse(sync_id=sync.id, status=sync.status)
        raise HTTPException(status.HTTP_409_CONFLICT, detail="sync is already finished")
    diagnostics = _merge_finish_diagnostics(sync.diagnostics_json or {}, body.diagnostics or {})
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

    pruned_document_ids: list[uuid.UUID] = []
    prune_skipped = await _stale_prune_skip_reason(db, sync, body, effective_status)
    if prune_skipped:
        diagnostics["stale_prune_skipped"] = prune_skipped
    else:
        pruned = await prune_stale_documents(db, source.project_id, source.id, sync.id)
        pruned_document_ids = pruned.document_ids
        diagnostics["stale_documents_deleted"] = len(pruned.document_ids)
        diagnostics["stale_chunks_deleted"] = pruned.chunks_deleted
    sync.diagnostics_json = diagnostics
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
            "stale_documents_deleted": len(pruned_document_ids),
        },
    )
    await db.flush()
    await db.commit()
    if pruned_document_ids:
        # Postgres is authoritative and already committed; orphaned points are
        # dropped at read time, so a Qdrant failure here is recorded, not raised.
        vectors_deleted = await delete_document_vectors(
            source.project_id, source.id, pruned_document_ids, settings
        )
        if not vectors_deleted:
            sync.diagnostics_json = {**diagnostics, "stale_vector_cleanup_failed": True}
            await db.commit()
        await invalidate_project(source.project_id)
    return SyncStatusResponse(sync_id=sync.id, status=sync.status)


@router.get("/sources/{source_id}/syncs/latest", response_model=SyncResponse)
async def get_latest_sync(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    source = await _require_accessible_source(db, source_id, user, settings, ProjectRole.viewer)
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
        _reject_anonymous(user, settings)
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Sync not found")
    await _ensure_access_hiding_existence(db, sync.project_id, user, settings, ProjectRole.viewer, "Sync not found")
    return _sync_response(sync)


async def _require_source(db: AsyncSession, source_id: uuid.UUID) -> Source:
    result = await db.execute(select(Source).where(Source.id == source_id))
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source not found")
    return source


async def _stale_prune_skip_reason(
    db: AsyncSession,
    sync: SourceSync,
    body: SyncFinishRequest,
    effective_status: str,
) -> str | None:
    """Say why this sync must not delete unseen documents; None means it may.

    Pruning is opt-in and only follows a clean, whole-source sync. Subset runs
    (--max-files, path filters) finish "success" too, and any rejected document
    (effective_status is then partial_success) means the sync's view is suspect.
    """
    if not body.complete:
        return "sync_not_complete"
    if effective_status != "success":
        return "sync_not_successful"
    skipped_reasons = (body.diagnostics or {}).get("skipped_reasons") or {}
    if not isinstance(skipped_reasons, dict) or any(
        count and reason not in POLICY_SKIP_REASONS for reason, count in skipped_reasons.items()
    ):
        return "sync_had_failed_files"
    if sync.documents_received <= 0:
        # Never wipe a source because a collector sent nothing.
        return "no_documents_received"
    # Exhausted retries do not touch parser_errors, so check the job rows too.
    failed_jobs = await _count(
        db,
        select(func.count()).select_from(IndexJob).where(IndexJob.sync_id == sync.id, IndexJob.status == "failed"),
    )
    return "sync_had_failed_index_jobs" if failed_jobs else None


async def _require_accessible_source(
    db: AsyncSession,
    source_id: uuid.UUID,
    user,
    settings: Settings,
    minimum_role: ProjectRole,
) -> Source:
    """Load a source for a route keyed only by source id, without confirming it exists.

    The project is only known from the row, so the lookup has to come first. A
    caller without access gets the same answer as for a missing source: 401 when
    anonymous, otherwise 404. Denials are still audited by the role check.
    """
    try:
        source = await _require_source(db, source_id)
    except HTTPException:
        _reject_anonymous(user, settings)
        raise
    await _ensure_access_hiding_existence(db, source.project_id, user, settings, minimum_role, "Source not found")
    return source


async def _ensure_access_hiding_existence(
    db: AsyncSession,
    project_id: uuid.UUID,
    user,
    settings: Settings,
    minimum_role: ProjectRole,
    not_found_detail: str,
) -> None:
    """Answer 404 to non-members. Members lacking the role keep their 403: they can
    already list the resource, so a 404 would hide nothing and only mislead them.
    """
    try:
        await ensure_project_access(db, project_id, user, settings, minimum_role=minimum_role)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_403_FORBIDDEN:
            raise
        if await get_project_role(db, project_id, user.id) is not None:
            raise
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=not_found_detail) from exc


def _reject_anonymous(user, settings: Settings) -> None:
    if user is None and (settings.is_production_like or not settings.demo_mode_public):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


async def _require_sync(
    db: AsyncSession,
    sync_id: uuid.UUID,
    source_id: uuid.UUID,
    for_update: bool = False,
) -> SourceSync:
    statement = select(SourceSync).where(
        SourceSync.id == sync_id,
        SourceSync.source_id == source_id,
    )
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
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
    diagnostics["last_batch_parser_error_reasons"] = dict(
        result.diagnostics.get("parser_error_reasons") or failure_reason_counts(error.code for error in result.errors)
    )
    diagnostics["last_batch_chunk_discard_reasons"] = dict(result.diagnostics.get("chunk_discard_reasons") or {})
    diagnostics["last_batch_embedding_failures"] = int(result.diagnostics.get("embedding_failures", 0) or 0)
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
    for counter in ("vector_publish_deferred", "stale_vector_cleanup_failures"):
        diagnostics[counter] = int(diagnostics.get(counter, 0) or 0) + int(result.diagnostics.get(counter, 0) or 0)
    diagnostics["warnings"] = result.warnings
    diagnostics["embedding_backend"] = result.embedding_backend
    return diagnostics


def _merge_finish_diagnostics(core: dict, collector: dict) -> dict:
    """Merge collector finish diagnostics into Core's without losing Core counts.

    Keys only the collector reports are added. For keys both sides report,
    counters take the larger value (per key for count maps), lists are unioned
    and other values keep Core's. Differing collector values are kept under
    ``collector_reported`` for inspection.
    """
    merged = dict(core)
    reported: dict = dict(merged.get("collector_reported") or {})
    for key, value in collector.items():
        if key not in merged or merged[key] in (None, "", [], {}):
            merged[key] = value
            continue
        current = merged[key]
        if current != value:
            reported[key] = value
        if _is_count(current) and _is_count(value):
            merged[key] = max(int(current), int(value))
        elif isinstance(current, dict) and isinstance(value, dict):
            combined = dict(current)
            for sub_key, sub_value in value.items():
                existing = combined.get(sub_key)
                if _is_count(existing) and _is_count(sub_value):
                    combined[sub_key] = max(int(existing), int(sub_value))
                elif existing is None:
                    combined[sub_key] = sub_value
            merged[key] = combined
        elif isinstance(current, list) and isinstance(value, list):
            merged[key] = current + [item for item in value if item not in current]
    if reported:
        merged["collector_reported"] = reported
    return merged


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def _mark_documents_seen(
    db: AsyncSession,
    source: Source,
    sync_id: uuid.UUID,
    external_ids: list[str],
) -> None:
    unique_ids = list(dict.fromkeys(external_id for external_id in external_ids if external_id))
    for start in range(0, len(unique_ids), _SEEN_MARK_BATCH_SIZE):
        await db.execute(
            update(Document)
            .where(
                Document.project_id == source.project_id,
                Document.source_id == source.id,
                Document.external_id.in_(unique_ids[start : start + _SEEN_MARK_BATCH_SIZE]),
            )
            .values(last_seen_sync_id=sync_id)
        )


async def _existing_document_hashes(
    db: AsyncSession,
    source: Source,
    external_ids: list[str],
) -> dict[str, str | None]:
    if not external_ids:
        return {}
    result = await db.execute(
        select(Document.external_id, Document.content_hash).where(
            Document.project_id == source.project_id,
            Document.source_id == source.id,
            Document.external_id.in_(external_ids),
        )
    )
    return {str(external_id): content_hash for external_id, content_hash in result.all()}


async def _reembed_source_chunks(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    settings: Settings,
) -> int:
    """Re-embed existing chunks in place; point ids are chunk ids, so upserts replace.

    Postgres is not changed here, so a failure part way leaves valid vectors for
    every chunk and the request can simply be repeated.
    """
    store = QdrantVectorStore(settings)
    reembedded = 0
    after_id: uuid.UUID | None = None
    while True:
        statement = (
            select(Chunk, Document.path)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.project_id == project_id, Chunk.source_id == source_id)
            .order_by(Chunk.id)
            .limit(_REINDEX_PAGE_SIZE)
        )
        if after_id is not None:
            statement = statement.where(Chunk.id > after_id)
        rows = (await db.execute(statement)).all()
        if not rows:
            return reembedded
        try:
            embeddings = await embed_texts_async([chunk.text for chunk, _ in rows], model_name=settings.embedding_model)
        except Exception as exc:
            logger.warning("reindex embedding failed source_id=%s error=%s", source_id, exc.__class__.__name__)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Embedding backend unavailable") from exc
        if len(embeddings) != len(rows):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Embedding backend unavailable")
        points = [
            chunk_point(chunk, embedding, index_version=settings.vector_index_version, document_path=path)
            for (chunk, path), embedding in zip(rows, embeddings)
        ]
        try:
            await store.upsert(points, settings.embedding_dim)
        except VectorStoreError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Vector store update failed") from exc
        reembedded += len(rows)
        after_id = rows[-1][0].id


async def _queue_reindex_jobs(db: AsyncSession, source: Source, settings: Settings) -> dict[str, int]:
    """Queue one index job per document from its stored payload for the dispatcher.

    Core keeps no raw document content outside index job payloads. A payload is
    used only if it matches the document's current content, or the job would roll
    the document back; documents without one (indexed inline) are counted, not
    touched, and a later sync or inline reindex covers them.
    """
    queued = without_payload = already_pending = 0
    after_id: uuid.UUID | None = None
    while True:
        statement = (
            select(Document)
            .where(Document.project_id == source.project_id, Document.source_id == source.id)
            .order_by(Document.id)
            .limit(_REINDEX_PAGE_SIZE)
        )
        if after_id is not None:
            statement = statement.where(Document.id > after_id)
        documents = (await db.execute(statement)).scalars().all()
        if not documents:
            return {
                "index_jobs_queued": queued,
                "documents_without_stored_payload": without_payload,
                "documents_already_pending": already_pending,
            }
        # content_hash is NULL while a publish is outstanding; checksum keeps the value.
        hashes = {document.content_hash or document.checksum for document in documents} - {None}
        stored_jobs = (
            await db.execute(
                select(IndexJob)
                .where(
                    IndexJob.project_id == source.project_id,
                    IndexJob.source_id == source.id,
                    IndexJob.content_hash.in_(hashes),
                )
                .order_by(IndexJob.created_at.desc())
            )
        ).scalars().all()
        payloads: dict[tuple[str | None, str], dict] = {}
        active: set[tuple[str | None, str]] = set()
        for job in stored_jobs:
            payload = job.normalized_payload_json or {}
            key = (payload.get("external_id"), job.content_hash)
            payloads.setdefault(key, payload)
            if job.status in ACTIVE_INDEX_JOB_STATUSES:
                active.add(key)
        for document in documents:
            content_hash = document.content_hash or document.checksum
            key = (document.external_id, content_hash)
            if key in active:
                already_pending += 1
                continue
            payload = payloads.get(key)
            if payload is None:
                without_payload += 1
                continue
            # Cleared so the worker re-embeds it instead of skipping it as unchanged.
            document.content_hash = None
            db.add(
                IndexJob(
                    project_id=source.project_id,
                    source_id=source.id,
                    # No sync: a reindex must not add to the last sync's counters.
                    sync_id=None,
                    normalized_payload_json={**payload, INDEX_REASON_KEY: INDEX_REASON_REINDEX},
                    content_hash=content_hash,
                    index_version=settings.vector_index_version,
                    status="pending",
                    attempts=0,
                )
            )
            queued += 1
        after_id = documents[-1].id


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

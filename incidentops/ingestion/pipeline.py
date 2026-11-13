"""
Ingestion pipeline for arbitrary project folders.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.collector.discovery import discover_files, normalize_file, skip_reason
from incidentops.collector.models import EVIDENCE_LOSS_SKIPS, DiscoveredFile
from incidentops.config.settings import Settings, get_settings
from incidentops.db.models import Source, SourceSync
from incidentops.ingestion.diagnostics import build_source_coverage
from incidentops.ingestion.indexer import index_normalized_documents
from incidentops.ingestion.normalized import BatchIngestResult, LocalIngestReadResult, NormalizedDocument

logger = logging.getLogger("incidentops.ingestion")

_MAX_LISTED_SKIPS = 500
# The local ingest API already publishes these labels for the Collector's reasons.
_LOCAL_SKIP_LABELS = {"oversized": "file_too_large", "empty": "empty_file", "read_failed": "read_error"}


async def run_ingestion(
    db: AsyncSession,
    project_id: uuid.UUID,
    base_path: str,
    embed_fn: Any | None = None,
) -> dict:
    start = time.time()
    settings = get_settings()
    base = Path(base_path)
    if not base.exists():
        raise FileNotFoundError(f"Ingestion path not found: {base_path}")

    source = await _get_or_create_local_source(db, project_id, base)
    sync = SourceSync(
        project_id=project_id,
        source_id=source.id,
        status="running",
        started_at=_utcnow(),
        diagnostics_json={},
    )
    db.add(sync)
    await db.flush()
    # Durable before any work starts, so a crash below still has a row to mark failed.
    await db.commit()
    source_id, sync_id = source.id, sync.id

    try:
        read_result = _read_local_documents(base, settings)
        index_result = await index_normalized_documents(
            db,
            project_id,
            source_id,
            sync_id,
            read_result.documents,
            embed_fn=embed_fn,
        )
    except Exception as exc:
        await _record_failed_ingest(db, sync_id, source_id, exc)
        raise

    parser_errors = list(read_result.parser_errors)
    skipped_files = list(read_result.skipped_files)
    for error in index_result.errors:
        parser_errors.append(
            {
                "path": error.path or error.external_id or "<unknown>",
                "error_summary": error.error,
            }
        )
        skipped_files.append(
            {
                "path": error.path or error.external_id or "<unknown>",
                "reason": "no_chunks_parsed" if error.error == "no chunks parsed" else "index_error",
            }
        )

    source_type_counts = dict(read_result.source_type_counts)
    chunk_type_counts = dict(index_result.chunk_type_counts)
    source_coverage = build_source_coverage(source_type_counts, chunk_type_counts)
    elapsed_ms = int((time.time() - start) * 1000)

    files_skipped = read_result.files_skipped + len(index_result.errors)
    sync.status, sync.error_message = _sync_outcome(parser_errors, index_result)
    sync.files_seen = read_result.total_files_seen
    sync.documents_received = index_result.received
    sync.chunks_created = index_result.chunks_created
    sync.files_skipped = files_skipped
    sync.parser_errors = len(parser_errors)
    sync.coverage_json = source_coverage
    sync.diagnostics_json = {
        "files_ingested": read_result.files_ingested,
        "skipped_files": skipped_files,
        "parser_errors": parser_errors,
        "documents_created": index_result.created,
        "documents_updated": index_result.updated,
        "skipped_unchanged": index_result.skipped_unchanged,
        "skipped_invalid": index_result.skipped_invalid,
        "source_type_counts": source_type_counts,
        "chunk_type_counts": chunk_type_counts,
        "warnings": source_coverage["warnings"],
        "embedding_backend": index_result.embedding_backend,
    }
    sync.finished_at = _utcnow()

    source.version_hash = _dir_hash(base)
    source.status = "error" if sync.status == "failed" else "ready"
    source.last_sync_started_at = sync.started_at
    source.last_sync_finished_at = sync.finished_at
    source.last_sync_status = sync.status
    source.last_error = sync.error_message

    logger.info(
        "Ingestion %s: %d docs processed, %d chunks, %d skipped files in %dms",
        sync.status,
        read_result.files_ingested,
        index_result.chunks_created,
        files_skipped,
        elapsed_ms,
    )
    return {
        "project_id": str(project_id),
        "status": sync.status,
        "documents_ingested": read_result.files_ingested,
        "total_files_seen": read_result.total_files_seen,
        "files_ingested": read_result.files_ingested,
        "files_skipped": files_skipped,
        "chunks_created": index_result.chunks_created,
        "duration_ms": elapsed_ms,
        "skipped_files": skipped_files,
        "parser_errors": parser_errors,
        "source_type_counts": source_type_counts,
        "chunk_type_counts": chunk_type_counts,
        "source_coverage": source_coverage,
    }


async def _get_or_create_local_source(db: AsyncSession, project_id: uuid.UUID, base: Path) -> Source:
    source_uri = str(base.resolve())
    result = await db.execute(
        select(Source).where(
            Source.project_id == project_id,
            Source.source_type == "filesystem",
            Source.source_uri == source_uri,
        )
    )
    source = result.scalar_one_or_none()
    if source:
        source.name = source.name or base.name or source_uri
        source.status = "active"
        source.sync_mode = source.sync_mode or "manual"
        source.source_uri = source_uri
        return source

    source = Source(
        project_id=project_id,
        name=base.name or source_uri,
        source_type="filesystem",
        status="active",
        sync_mode="manual",
        source_uri=source_uri,
        config_json={"mode": "local_path"},
        version_hash=_dir_hash(base),
    )
    db.add(source)
    await db.flush()
    return source


def _read_local_documents(base: Path, settings: Settings) -> LocalIngestReadResult:
    """Apply the Collector's discovery, skip, UTF-8 and redaction rules to a local folder."""
    result = LocalIngestReadResult()
    source_type_counts: Counter[str] = Counter()
    documents: list[NormalizedDocument] = []

    def skip(item: DiscoveredFile, reason: str) -> None:
        result.files_skipped += 1
        # .git and node_modules can hold many thousands of files; the count stays exact.
        if len(result.skipped_files) < _MAX_LISTED_SKIPS:
            result.skipped_files.append(
                {
                    "path": item.relative_path,
                    "reason": _LOCAL_SKIP_LABELS.get(reason, reason),
                    "size_bytes": item.size_bytes,
                }
            )
        if reason in EVIDENCE_LOSS_SKIPS:
            result.parser_errors.append({"path": item.relative_path, "error_summary": reason})

    for item in discover_files(base):
        result.total_files_seen += 1
        reason = skip_reason(item, settings, max_bytes=settings.max_ingest_file_bytes)
        if reason:
            skip(item, reason)
            continue
        document, normalize_reason = normalize_file(item)
        if document is None:
            skip(item, normalize_reason or "malformed_content")
            continue
        source_type_counts[document["source_type"]] += 1
        documents.append(NormalizedDocument(**document))
        result.files_ingested += 1

    logger.info("Discovered %d files in %s", result.total_files_seen, str(base))
    result.documents = documents
    result.source_type_counts = dict(source_type_counts)
    return result


def _sync_outcome(parser_errors: list[dict], index_result: BatchIngestResult) -> tuple[str, str | None]:
    """Status of a finished run, and the error message when nothing could be indexed."""
    if not parser_errors:
        return "success", None
    if index_result.created or index_result.updated or index_result.skipped_unchanged:
        return "partial_success", None
    return "failed", f"local ingest failed: no documents indexed, {len(parser_errors)} errors"


async def _record_failed_ingest(db: AsyncSession, sync_id: uuid.UUID, source_id: uuid.UUID, exc: Exception) -> None:
    """Persist a failed sync after an exception, without masking that exception."""
    error = type(exc).__name__
    message = f"local ingest failed: {error}"
    try:
        await db.rollback()
        sync = await db.get(SourceSync, sync_id)
        source = await db.get(Source, source_id)
        sync.status = "failed"
        sync.finished_at = _utcnow()
        sync.error_message = message
        sync.diagnostics_json = {"error": error, "error_message": message}
        source.status = "error"
        source.last_sync_started_at = sync.started_at
        source.last_sync_finished_at = sync.finished_at
        source.last_sync_status = "failed"
        source.last_error = message
        await db.commit()
    except Exception as record_exc:
        logger.error("could not mark sync %s failed: %s", sync_id, type(record_exc).__name__)


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _dir_hash(base: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(base.rglob("*")):
        if path.is_file():
            h.update(str(path).encode())
            h.update(str(path.stat().st_mtime).encode())
    return h.hexdigest()[:16]

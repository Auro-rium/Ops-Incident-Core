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

from incidentops.config.settings import get_settings
from incidentops.db.models import Source, SourceSync
from incidentops.ingestion.chunking.metadata import classify_source_type, extract_service_from_path
from incidentops.ingestion.diagnostics import build_source_coverage
from incidentops.ingestion.indexer import index_normalized_documents
from incidentops.ingestion.normalized import LocalIngestReadResult, NormalizedDocument

logger = logging.getLogger("incidentops.ingestion")


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

    read_result = _read_local_documents(base, settings.max_ingest_file_bytes, settings.supported_extensions_set)
    index_result = await index_normalized_documents(
        db,
        project_id,
        source.id,
        sync.id,
        read_result.documents,
        embed_fn=embed_fn,
    )

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

    sync.status = "success"
    sync.files_seen = read_result.total_files_seen
    sync.documents_received = index_result.received
    sync.chunks_created = index_result.chunks_created
    sync.files_skipped = len(skipped_files)
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
    source.status = "ready"
    source.last_sync_started_at = sync.started_at
    source.last_sync_finished_at = sync.finished_at
    source.last_sync_status = sync.status
    source.last_error = None

    logger.info(
        "Ingestion complete: %d docs processed, %d chunks, %d skipped files in %dms",
        read_result.files_ingested,
        index_result.chunks_created,
        len(skipped_files),
        elapsed_ms,
    )
    return {
        "project_id": str(project_id),
        "documents_ingested": read_result.files_ingested,
        "total_files_seen": read_result.total_files_seen,
        "files_ingested": read_result.files_ingested,
        "files_skipped": len(skipped_files),
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


def _read_local_documents(base: Path, max_ingest_file_bytes: int, supported_extensions: set[str]) -> LocalIngestReadResult:
    files = _discover_files(base)
    logger.info("Discovered %d candidate files in %s", len(files), str(base))

    result = LocalIngestReadResult(total_files_seen=len(files))
    source_type_counts: Counter[str] = Counter()
    documents: list[NormalizedDocument] = []

    for file_path in files:
        rel_path = str(file_path.relative_to(base))
        suffix = file_path.suffix.lower()
        file_size = file_path.stat().st_size
        if suffix not in supported_extensions:
            result.files_skipped += 1
            result.skipped_files.append({"path": rel_path, "reason": "unsupported_extension", "size_bytes": file_size})
            logger.info("Skipping unsupported file: %s", rel_path)
            continue
        if file_size > max_ingest_file_bytes:
            result.files_skipped += 1
            result.skipped_files.append({"path": rel_path, "reason": "file_too_large", "size_bytes": file_size})
            logger.warning("Skipping large file: %s (%d bytes)", rel_path, file_size)
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            result.files_skipped += 1
            result.skipped_files.append({"path": rel_path, "reason": "read_error", "detail": str(exc)})
            logger.warning("Skipping unreadable file %s: %s", rel_path, exc)
            continue
        if not content.strip():
            result.files_skipped += 1
            result.skipped_files.append({"path": rel_path, "reason": "empty_file"})
            continue

        source_type = classify_source_type(rel_path)
        source_type_counts[source_type] += 1
        documents.append(
            NormalizedDocument(
                external_id=rel_path,
                path=rel_path,
                source_type=source_type,
                content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                metadata={"service_name": extract_service_from_path(rel_path)},
                size_bytes=file_size,
                modified_at=_timestamp_from_stat(file_path),
            )
        )
        result.files_ingested += 1

    result.documents = documents
    result.source_type_counts = dict(source_type_counts)
    return result


def _discover_files(base: Path) -> list[Path]:
    files = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in str(path) or path.name.startswith("."):
            continue
        files.append(path)
    return files


def _timestamp_from_stat(path: Path):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


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

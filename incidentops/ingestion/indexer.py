from __future__ import annotations

import asyncio
import inspect
import logging
import math
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.config.settings import get_settings
from incidentops.db.models import Chunk, Document, EvidenceRelation, IndexJob, RetrievalResult, SourceSync
from incidentops.ingestion.chunking.chunker import count_tokens, split_text_by_tokens
from incidentops.ingestion.chunking.metadata import classify_doc_type, classify_source_type, extract_service_from_path
from incidentops.ingestion.diagnostics import build_source_coverage
from incidentops.ingestion.failure_taxonomy import (
    FAILURE_CHUNK_LIMIT_EXCEEDED,
    FAILURE_EMBEDDING_FAILED,
    FAILURE_PARSER_EXCEPTION,
    FAILURE_VECTOR_INDEX_FAILED,
    failure_reason_counts,
    normalize_failure_code,
)
from incidentops.ingestion.normalized import (
    BatchIngestResult,
    DocumentBatchError,
    NormalizedDocument,
    validate_normalized_document,
)
from incidentops.ingestion.parsers.code_parser import (
    parse_go,
    parse_java,
    parse_jsts,
    parse_proto,
    parse_python,
    parse_structured_config,
)
from incidentops.ingestion.parsers.deploy_parser import parse_deploy_history, parse_patch_file
from incidentops.ingestion.parsers.incident_parser import parse_incident
from incidentops.ingestion.parsers.log_parser import parse_logs
from incidentops.ingestion.parsers.markdown_parser import parse_markdown
from incidentops.ingestion.schemas import RawChunk
from incidentops.retrieval.graph_relations import build_chunk_relations
from incidentops.retrieval.cache import invalidate_project
from incidentops.retrieval.vector_store import QdrantVectorStore, VectorStoreError, chunk_point

logger = logging.getLogger("incidentops.ingestion.indexer")

# Index jobs created by Core itself (not by a collector batch) carry this key in
# their normalized payload. The worker treats them as "re-publish this exact
# content" and marks them stale if the document has been rewritten since.
INDEX_REASON_KEY = "_index_reason"
INDEX_REASON_VECTOR_RETRY = "vector_retry"
INDEX_REASON_REINDEX = "reindex"
_PRUNE_BATCH_SIZE = 500


@dataclass
class _VectorPublication:
    """Vectors for one document, published to Qdrant after Postgres commits."""

    document_id: uuid.UUID
    normalized: NormalizedDocument
    points: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PrunedDocuments:
    document_ids: list[uuid.UUID] = field(default_factory=list)
    chunks_deleted: int = 0


async def index_normalized_documents(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    sync_id: uuid.UUID | None,
    documents: list[NormalizedDocument],
    *,
    embed_fn: Any | None = None,
    mark_existing_seen: bool = True,
    vector_retry_attempts: int = 0,
) -> BatchIngestResult:
    """Index documents into Postgres, then publish their vectors to Qdrant.

    Postgres is committed before Qdrant is touched, so a vector-store failure
    can never leave chunks missing for points that exist. Points without a
    chunk are dropped at read time; a failed publish queues a retry IndexJob.

    A document's ``content_hash`` is committed as NULL with its chunks and only
    set once its vectors are live. If the process dies or is cancelled in between,
    the next attempt or sync reprocesses it instead of skipping it as unchanged.

    ``mark_existing_seen=False`` only moves ``last_seen_sync_id`` of existing rows
    forward (worker jobs: the batch route marked them when queued, and a late job
    from an older sync must not move that back).
    ``vector_retry_attempts`` seeds ``attempts`` on retry jobs created here.
    """
    settings = get_settings()
    result = BatchIngestResult(received=len(documents), embedding_backend=settings.embedding_model if embed_fn else None)
    if not documents:
        return result

    source_type_counts: Counter[str] = Counter()
    chunk_type_counts: Counter[str] = Counter()
    seen_external_ids: set[str] = set()
    publications: list[_VectorPublication] = []
    for normalized in documents:
        validation_result = validate_normalized_document(normalized, settings)
        if isinstance(validation_result, DocumentBatchError):
            result.skipped_invalid += 1
            result.errors.append(validation_result)
            continue
        normalized = validation_result
        if normalized.external_id in seen_external_ids:
            result.skipped_invalid += 1
            result.errors.append(
                DocumentBatchError(
                    external_id=normalized.external_id,
                    path=normalized.path,
                    code=normalize_failure_code("duplicate_external_id"),
                    error="duplicate external_id in batch",
                    message="duplicate external_id in batch",
                )
            )
            continue
        seen_external_ids.add(normalized.external_id)
        try:
            operation = await _index_one_document(
                db,
                project_id,
                source_id,
                normalized,
                embed_fn=embed_fn,
                settings=settings,
                sync_id=sync_id,
                mark_existing_seen=mark_existing_seen,
            )
        except DocumentIndexingError as exc:
            result.skipped_invalid += 1
            result.errors.append(
                DocumentBatchError(
                    external_id=normalized.external_id,
                    path=normalized.path,
                    code=exc.code,
                    error=exc.safe_message,
                    message=exc.safe_message,
                )
            )
            continue
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Failed indexing normalized document %s", normalized.path)
            result.skipped_invalid += 1
            result.errors.append(
                DocumentBatchError(
                    external_id=normalized.external_id,
                    path=normalized.path,
                    code=normalize_failure_code("index_error"),
                    error=f"failed to index document: {exc.__class__.__name__}",
                    message="failed to index document",
                )
            )
            continue

        result.created += operation["created"]
        result.updated += operation["updated"]
        result.skipped_unchanged += operation["skipped_unchanged"]
        result.chunks_created += operation["chunks_created"]
        if operation["created"] or operation["updated"]:
            source_type_counts[normalized.source_type] += 1
        chunk_type_counts.update(operation["chunk_type_counts"])
        if operation.get("publication") is not None:
            publications.append(operation["publication"])

    vector_publish_deferred = 0
    stale_vector_cleanup_failures = 0
    if publications:
        # Make the new chunks durable first; only then replace vectors.
        await db.commit()
        vector_publish_deferred, stale_vector_cleanup_failures = await _publish_vectors(
            db,
            project_id,
            source_id,
            sync_id,
            publications,
            settings=settings,
            retry_attempts=vector_retry_attempts,
        )
        await db.commit()

    result.source_type_counts = dict(source_type_counts)
    result.chunk_type_counts = dict(chunk_type_counts)
    result.coverage = build_source_coverage(result.source_type_counts, result.chunk_type_counts)
    result.warnings = result.coverage.get("warnings", [])
    parser_error_reasons = failure_reason_counts(error.code for error in result.errors)
    result.diagnostics = {
        "documents_created": result.created,
        "documents_updated": result.updated,
        "skipped_unchanged": result.skipped_unchanged,
        "skipped_invalid": result.skipped_invalid,
        "errors": len(result.errors),
        "parser_error_count": len(result.errors),
        "parser_error_reasons": parser_error_reasons,
        "chunk_discard_reasons": {
            FAILURE_CHUNK_LIMIT_EXCEEDED: parser_error_reasons.get(FAILURE_CHUNK_LIMIT_EXCEEDED, 0)
        },
        "embedding_failures": parser_error_reasons.get(FAILURE_EMBEDDING_FAILED, 0),
        "vector_index_failures": parser_error_reasons.get(FAILURE_VECTOR_INDEX_FAILED, 0),
        "source_type_counts": result.source_type_counts,
        "chunk_type_counts": result.chunk_type_counts,
        "embedding_backend": result.embedding_backend,
        "vector_publish_deferred": vector_publish_deferred,
        "stale_vector_cleanup_failures": stale_vector_cleanup_failures,
    }
    if result.created or result.updated:
        await invalidate_project(project_id)
    return result


async def _publish_vectors(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    sync_id: uuid.UUID | None,
    publications: list[_VectorPublication],
    *,
    settings: Any,
    retry_attempts: int,
) -> tuple[int, int]:
    """Upsert new vectors, then drop the document's stale points.

    Returns (deferred publishes, stale-point cleanup failures). A successful
    upsert restores the document's content_hash. A failed one leaves it NULL (so
    neither the retry nor the next sync skips it as unchanged) and queues an
    IndexJob for the dispatcher. The caller commits.
    """
    store = QdrantVectorStore(settings)
    deferred = 0
    cleanup_failures = 0
    for publication in publications:
        try:
            await store.upsert(publication.points, settings.embedding_dim)
        except VectorStoreError:
            logger.warning(
                "vector publish failed; queueing retry document_id=%s", publication.document_id
            )
            deferred += 1
            payload = publication.normalized.model_dump(mode="json")
            payload[INDEX_REASON_KEY] = INDEX_REASON_VECTOR_RETRY
            retryable = retry_attempts < settings.rag_index_max_retries
            db.add(
                IndexJob(
                    project_id=project_id,
                    source_id=source_id,
                    sync_id=sync_id,
                    normalized_payload_json=payload,
                    content_hash=publication.normalized.content_hash,
                    index_version=settings.vector_index_version,
                    status="pending" if retryable else "failed",
                    attempts=retry_attempts,
                    error_code=FAILURE_VECTOR_INDEX_FAILED,
                    finished_at=None if retryable else datetime.now(timezone.utc),
                )
            )
            continue
        # The checksum match keeps a concurrent rewrite's NULL in place.
        await db.execute(
            update(Document)
            .where(
                Document.id == publication.document_id,
                Document.checksum == publication.normalized.content_hash,
            )
            .values(content_hash=publication.normalized.content_hash)
        )
        try:
            await store.delete_document_points_except(
                project_id,
                str(publication.document_id),
                [point["id"] for point in publication.points],
            )
        except VectorStoreError:
            # New vectors are live; leftover points have no chunk and are
            # filtered at read time, so this is logged rather than retried.
            cleanup_failures += 1
            logger.warning("stale vector cleanup failed document_id=%s", publication.document_id)
    return deferred, cleanup_failures


def _advance_seen_marker(document_id: uuid.UUID, sync_id: uuid.UUID):
    """Claim a document for a sync unless a sync started at or after it already has.

    A marker naming a sync that no longer exists counts as older.
    """
    started_at = select(SourceSync.started_at).where(SourceSync.id == sync_id).scalar_subquery()
    same_or_newer = select(SourceSync.id).where(SourceSync.started_at >= started_at)
    return (
        update(Document)
        .where(
            Document.id == document_id,
            or_(Document.last_seen_sync_id.is_(None), Document.last_seen_sync_id.not_in(same_or_newer)),
        )
        .values(last_seen_sync_id=sync_id)
    )


async def prune_stale_documents(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    sync_id: uuid.UUID,
) -> PrunedDocuments:
    """Delete a source's documents that the given (complete) sync did not see.

    Removes chunks, evidence relations and retrieval results explicitly; the
    caller commits, then calls ``delete_document_vectors`` for Qdrant.
    """
    stale_result = await db.execute(
        select(Document.id).where(
            Document.project_id == project_id,
            Document.source_id == source_id,
            (Document.last_seen_sync_id.is_(None)) | (Document.last_seen_sync_id != sync_id),
        )
    )
    document_ids = list(stale_result.scalars().all())
    pruned = PrunedDocuments(document_ids=document_ids)
    if not document_ids:
        return pruned
    for start in range(0, len(document_ids), _PRUNE_BATCH_SIZE):
        batch = document_ids[start : start + _PRUNE_BATCH_SIZE]
        chunk_result = await db.execute(select(Chunk.id).where(Chunk.document_id.in_(batch)))
        chunk_ids = list(chunk_result.scalars().all())
        pruned.chunks_deleted += len(chunk_ids)
        if chunk_ids:
            await db.execute(delete(EvidenceRelation).where(EvidenceRelation.evidence_chunk_id.in_(chunk_ids)))
            await db.execute(delete(RetrievalResult).where(RetrievalResult.chunk_id.in_(chunk_ids)))
            await db.execute(delete(Chunk).where(Chunk.id.in_(chunk_ids)))
        await db.execute(
            delete(Document).where(
                Document.project_id == project_id,
                Document.source_id == source_id,
                Document.id.in_(batch),
            )
        )
    return pruned


async def delete_document_vectors(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    document_ids: list[uuid.UUID],
    settings: Any,
) -> bool:
    """Best-effort Qdrant cleanup for removed documents (all index versions)."""
    store = QdrantVectorStore(settings)
    ok = True
    for start in range(0, len(document_ids), _PRUNE_BATCH_SIZE):
        batch = [str(document_id) for document_id in document_ids[start : start + _PRUNE_BATCH_SIZE]]
        try:
            await store.delete_by_filter(
                project_id,
                all_index_versions=True,
                source_id=str(source_id),
                document_id=batch,
            )
        except VectorStoreError:
            ok = False
            logger.warning("stale document vector cleanup failed count=%s", len(batch))
    return ok


class DocumentIndexingError(Exception):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = normalize_failure_code(code)
        self.safe_message = safe_message


async def _index_one_document(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    normalized: NormalizedDocument,
    *,
    embed_fn: Any | None,
    settings: Any,
    sync_id: uuid.UUID | None = None,
    mark_existing_seen: bool = True,
) -> dict[str, Any]:
    existing_result = await db.execute(
        select(Document).where(
            Document.project_id == project_id,
            Document.source_id == source_id,
            Document.external_id == normalized.external_id,
        )
    )
    current = existing_result.scalar_one_or_none()
    if current is not None and sync_id is not None:
        # Received in this sync: never prune it, even if indexing it fails below.
        if mark_existing_seen:
            current.last_seen_sync_id = sync_id
        else:
            await db.execute(_advance_seen_marker(current.id, sync_id))
    if current and current.content_hash == normalized.content_hash:
        return {
            "created": 0,
            "updated": 0,
            "skipped_unchanged": 1,
            "chunks_created": 0,
            "chunk_type_counts": {},
        }

    service_name = normalized.metadata.get("service_name") or extract_service_from_path(normalized.path)
    try:
        raw_chunks, parse_error = _parse_normalized_document(normalized, service_name=service_name)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Failed parsing normalized document %s", normalized.path)
        raise DocumentIndexingError(
            FAILURE_PARSER_EXCEPTION,
            f"failed to parse document: {exc.__class__.__name__}",
        ) from exc

    if parse_error:
        raise DocumentIndexingError(FAILURE_PARSER_EXCEPTION, parse_error)
    if not raw_chunks:
        raise DocumentIndexingError("no_chunks_parsed", "no chunks parsed")
    if _estimate_chunk_segments(raw_chunks, settings.max_chunk_tokens) > settings.max_chunks_per_document:
        raw_chunks = _build_bounded_fallback_chunks(normalized, service_name=service_name, settings=settings)

    chunk_payloads: list[dict[str, Any]] = []
    embedding_texts: list[str] = []
    chunk_type_counts: Counter[str] = Counter()
    for raw_chunk in raw_chunks:
        sub_texts = (
            split_text_by_tokens(raw_chunk.text, settings.max_chunk_tokens)
            if count_tokens(raw_chunk.text) > settings.max_chunk_tokens
            else [raw_chunk.text]
        )
        for sub_text in sub_texts:
            if len(chunk_payloads) >= settings.max_chunks_per_document:
                raise DocumentIndexingError(
                    "too_many_chunks",
                    "document exceeds configured chunk count limit",
                )
            chunk_payload = {
                "project_id": project_id,
                "source_id": source_id,
                "chunk_type": raw_chunk.chunk_type,
                "service_name": raw_chunk.service_name or service_name or normalized.metadata.get("service_name"),
                "endpoint": raw_chunk.endpoint or normalized.metadata.get("endpoint"),
                "deploy_hash": _chunk_deploy_hash(normalized, raw_chunk),
                "timestamp_start": raw_chunk.timestamp_start,
                "timestamp_end": raw_chunk.timestamp_end,
                "section_title": raw_chunk.section_title,
                "start_line": raw_chunk.start_line,
                "end_line": raw_chunk.end_line,
                "text": sub_text,
                "token_count": count_tokens(sub_text),
                "metadata_json": _build_chunk_metadata(normalized, raw_chunk),
            }
            chunk_payloads.append(chunk_payload)
            embedding_texts.append(sub_text)
            chunk_type_counts[raw_chunk.chunk_type] += 1

    embeddings: list[list[float]] | None = None
    if embed_fn and chunk_payloads:
        try:
            if inspect.iscoroutinefunction(embed_fn):
                embeddings = await embed_fn(embedding_texts)
            else:
                embeddings = await asyncio.to_thread(embed_fn, embedding_texts)
        except Exception as exc:
            raise DocumentIndexingError("embedding_error", f"failed to embed document: {exc.__class__.__name__}") from exc
        if len(embeddings) != len(chunk_payloads):
            raise DocumentIndexingError("embedding_error", "embedding backend returned an unexpected result count")
        if any(len(embedding) != settings.embedding_dim for embedding in embeddings):
            raise DocumentIndexingError(
                FAILURE_EMBEDDING_FAILED,
                "embedding dimension does not match the configured vector index",
            )

    async with db.begin_nested():
        document_row = current
        created = 0
        updated = 0
        if document_row is None:
            document_row = Document(
                project_id=project_id,
                source_id=source_id,
                external_id=normalized.external_id,
                title=PurePosixPath(normalized.path).name,
                path=normalized.path,
                doc_type=_doc_type_for_normalized_document(normalized),
                source_type=normalized.source_type,
                checksum=normalized.content_hash,
                content_hash=normalized.content_hash,
                modified_at=normalized.modified_at,
                size_bytes=normalized.size_bytes,
                last_seen_sync_id=sync_id,
            )
            db.add(document_row)
            await db.flush()
            created = 1
        else:
            document_row.title = PurePosixPath(normalized.path).name
            document_row.path = normalized.path
            document_row.doc_type = _doc_type_for_normalized_document(normalized)
            document_row.source_type = normalized.source_type
            document_row.checksum = normalized.content_hash
            document_row.content_hash = normalized.content_hash
            document_row.modified_at = normalized.modified_at
            document_row.size_bytes = normalized.size_bytes
            updated = 1
            await db.execute(delete(Chunk).where(Chunk.document_id == document_row.id))

        persisted_chunks: list[Chunk] = []
        for chunk_payload in chunk_payloads:
            chunk_payload["document_id"] = document_row.id
            chunk = Chunk(**chunk_payload)
            db.add(chunk)
            persisted_chunks.append(chunk)
        await db.flush()
        relations = build_chunk_relations(document_row, persisted_chunks)
        if relations:
            db.add_all(EvidenceRelation(**relation) for relation in relations)
            await db.flush()
        await db.execute(
            update(Chunk)
            .where(Chunk.document_id == document_row.id)
            .values(search_tsvector=func.to_tsvector("english", Chunk.text))
        )
        await db.flush()
        # Build points now (chunk ids exist after flush); publish after commit.
        points = [
            chunk_point(
                chunk,
                embedding,
                index_version=settings.vector_index_version,
                document_path=document_row.path,
            )
            for chunk, embedding in zip(persisted_chunks, embeddings or [])
        ]
        publication = None
        if points or current is not None:
            # Even without new vectors, an updated document's old points are stale.
            publication = _VectorPublication(document_id=document_row.id, normalized=normalized, points=points)
            # Set again by _publish_vectors once the vectors are live.
            document_row.content_hash = None

    return {
        "created": created,
        "updated": updated,
        "skipped_unchanged": 0,
        "chunks_created": len(chunk_payloads),
        "chunk_type_counts": dict(chunk_type_counts),
        "publication": publication,
    }


def _estimate_chunk_segments(raw_chunks: list[RawChunk], max_chunk_tokens: int) -> int:
    total = 0
    for raw_chunk in raw_chunks:
        token_count = max(1, count_tokens(raw_chunk.text))
        total += max(1, math.ceil(token_count / max_chunk_tokens))
    return total


def _build_bounded_fallback_chunks(
    normalized: NormalizedDocument,
    *,
    service_name: str | None,
    settings: Any,
) -> list[RawChunk]:
    lines = normalized.content.splitlines()
    if not lines:
        return []
    total_tokens = max(1, count_tokens(normalized.content))
    target_chunk_count = min(
        settings.max_chunks_per_document,
        max(1, math.ceil(total_tokens / settings.max_chunk_tokens)),
    )
    target_tokens = max(
        settings.max_chunk_tokens,
        math.ceil(total_tokens / target_chunk_count),
    )
    chunk_type = _fallback_chunk_type(normalized.path, normalized.source_type)
    chunks: list[RawChunk] = []
    start_index = 0
    while start_index < len(lines) and len(chunks) < settings.max_chunks_per_document:
        end_index = start_index
        chunk_lines: list[str] = []
        while end_index < len(lines):
            candidate_lines = chunk_lines + [lines[end_index]]
            candidate_text = "\n".join(candidate_lines).strip()
            if chunk_lines and count_tokens(candidate_text) > target_tokens:
                break
            chunk_lines = candidate_lines
            end_index += 1
            if count_tokens("\n".join(chunk_lines).strip()) >= target_tokens:
                break
        chunk_text = "\n".join(chunk_lines).strip()
        if not chunk_text:
            break
        chunks.append(
            RawChunk(
                text=chunk_text,
                chunk_type=chunk_type,
                source_type=normalized.source_type,
                document_path=normalized.path,
                doc_type=_doc_type_for_normalized_document(normalized),
                service_name=service_name,
                section_title=PurePosixPath(normalized.path).name,
                start_line=start_index + 1,
                end_line=end_index,
                metadata={
                    "language": normalized.metadata.get("language") or normalized.metadata.get("file_language"),
                    "package_name": normalized.metadata.get("package_name"),
                    "symbol_name": normalized.metadata.get("symbol_name"),
                    "fallback_reason": "chunk_limit_guard",
                },
            )
        )
        start_index = end_index
    return chunks


def _fallback_chunk_type(path: str, source_type: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".go":
        return "go_fallback"
    if suffix == ".proto":
        return "proto_fallback"
    if source_type == "config":
        return "config_fallback"
    if source_type == "api_doc":
        return "api_fallback"
    if source_type == "code":
        return "code_fallback"
    return "text_fallback"


def _doc_type_for_normalized_document(document: NormalizedDocument) -> str:
    return classify_doc_type(document.path)


def summarize_source_types(documents: list[NormalizedDocument]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for document in documents:
        counts[document.source_type] += 1
    return dict(counts)


def _parse_normalized_document(
    document: NormalizedDocument,
    *,
    service_name: str | None,
):
    lower_path = document.path.lower()
    suffix = PurePosixPath(lower_path).suffix.lower()
    source_type = (document.source_type or classify_source_type(document.path)).lower()
    if source_type == "logs" or suffix == ".log":
        return parse_logs(document.content, document.path, service_name=service_name), None
    if suffix in {".patch", ".diff"}:
        return parse_patch_file(document.content, document.path), None
    if source_type == "deploy" and suffix == ".json":
        chunks = parse_deploy_history(document.content, document.path)
        return chunks, None if chunks else "invalid or unsupported deploy JSON structure"
    if suffix == ".json" and any(token in lower_path for token in ("deploy", "release", "commit")):
        chunks = parse_deploy_history(document.content, document.path)
        return chunks, None if chunks else "invalid or unsupported deploy JSON structure"
    if suffix == ".py":
        return parse_python(document.content, document.path, service_name=service_name), None
    if suffix == ".go":
        return parse_go(document.content, document.path, service_name=service_name), None
    if suffix in {".js", ".ts", ".jsx", ".tsx"}:
        language = {".jsx": "js", ".tsx": "ts"}.get(suffix, suffix.lstrip("."))
        return parse_jsts(document.content, document.path, service_name=service_name, language=language), None
    if suffix == ".java":
        return parse_java(document.content, document.path, service_name=service_name), None
    if suffix == ".proto":
        return parse_proto(document.content, document.path, service_name=service_name), None
    # Incident text is identified by an incidents/ or postmortems/ directory
    # segment, never by a substring such as "incidentops/".
    if source_type == "incident" or classify_source_type(document.path) == "incident":
        return parse_incident(document.content, document.path), None
    if source_type == "code":
        return _parse_generic_code(document.content, document.path, service_name=service_name), None
    if suffix in {".yaml", ".yml", ".toml", ".ini"}:
        hinted_source_type = "api_doc" if source_type == "api_doc" else source_type
        return parse_structured_config(document.content, document.path, source_type=hinted_source_type, service_name=service_name), None
    if suffix == ".json" and source_type in {"config", "api_doc"}:
        return parse_structured_config(document.content, document.path, source_type=source_type, service_name=service_name), None
    if suffix in {".md", ".txt", ".json"}:
        hinted_source_type = source_type or classify_source_type(document.path)
        return parse_markdown(document.content, document.path, source_type=hinted_source_type, service_name=service_name), None
    if source_type in {"config", "unknown_text", "runbook", "api_doc"}:
        return parse_markdown(document.content, document.path, source_type=source_type, service_name=service_name), None
    logger.info("No parser configured for normalized document %s", document.path)
    return [], "no parser configured"


def _chunk_deploy_hash(document: NormalizedDocument, raw_chunk: RawChunk) -> str | None:
    """A repository commit is deploy evidence only for deploy/patch documents."""
    explicit = raw_chunk.deploy_hash or document.metadata.get("deploy_hash")
    if explicit:
        return explicit
    is_deploy = (
        raw_chunk.source_type == "deploy"
        or (document.source_type or "").lower() == "deploy"
        or PurePosixPath(document.path.lower()).suffix in {".patch", ".diff"}
    )
    return document.metadata.get("commit_sha") if is_deploy else None


def _parse_generic_code(content: str, path: str, *, service_name: str | None) -> list[RawChunk]:
    lines = content.splitlines()
    if not lines:
        return []
    return [
        RawChunk(
            text=content,
            chunk_type="code_file",
            source_type="code",
            document_path=path,
            doc_type="code",
            service_name=service_name,
            section_title=PurePosixPath(path).name,
            start_line=1,
            end_line=len(lines),
            metadata={"parser": "generic_code"},
        )
    ]


_CHUNK_METADATA_KEYS = {
    "language",
    "file_language",
    "repo_name",
    "branch",
    "commit_sha",
    "commit_shas",
    "deploy_hash",
    "deploy_hashes",
    "module_path",
    "package_path",
    "package_name",
    "symbol_name",
    "symbol_names",
    "function_names",
    "class_names",
    "headings",
    "endpoint",
    "endpoint_candidates",
    "api_paths",
    "service_name",
    "log_levels",
    "timestamp_start",
    "timestamp_end",
    "error_codes",
    "trace_ids",
    "request_ids",
    "config_keys_summary",
    "release_markers",
    "title",
    "severity",
}


def _build_chunk_metadata(normalized: NormalizedDocument, raw_chunk: RawChunk) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source_type": raw_chunk.source_type,
        "document_path": raw_chunk.document_path,
        "content_hash": normalized.content_hash,
    }
    for key in _CHUNK_METADATA_KEYS:
        value = normalized.metadata.get(key)
        if value in (None, "", []):
            continue
        metadata[key] = value
    for key, value in (raw_chunk.metadata or {}).items():
        if value in (None, "", []):
            continue
        metadata[key] = value
    return metadata

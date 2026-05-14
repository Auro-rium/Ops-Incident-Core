from __future__ import annotations

import logging
import uuid
from collections import Counter
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.config.settings import get_settings
from incidentops.db.models import Chunk, Document
from incidentops.ingestion.chunking.chunker import count_tokens, split_text_by_tokens
from incidentops.ingestion.chunking.metadata import classify_doc_type, classify_source_type, extract_service_from_path
from incidentops.ingestion.normalized import BatchIngestResult, DocumentBatchError, NormalizedDocument
from incidentops.ingestion.parsers.code_parser import parse_python
from incidentops.ingestion.parsers.deploy_parser import parse_deploy_history, parse_patch_file
from incidentops.ingestion.parsers.incident_parser import parse_incident
from incidentops.ingestion.parsers.log_parser import parse_logs
from incidentops.ingestion.parsers.markdown_parser import parse_markdown

logger = logging.getLogger("incidentops.ingestion.indexer")


async def index_normalized_documents(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    sync_id: uuid.UUID | None,
    documents: list[NormalizedDocument],
    *,
    embed_fn: Any | None = None,
) -> BatchIngestResult:
    settings = get_settings()
    result = BatchIngestResult(received=len(documents))
    if not documents:
        return result

    external_ids = [document.external_id for document in documents]
    existing_result = await db.execute(
        select(Document).where(
            Document.project_id == project_id,
            Document.source_id == source_id,
            Document.external_id.in_(external_ids),
        )
    )
    existing_by_external_id = {
        document.external_id: document
        for document in existing_result.scalars().all()
        if document.external_id
    }

    prepared_chunks: list[dict[str, Any]] = []
    prepared_embeddings_text: list[str] = []
    document_operations: list[dict[str, Any]] = []

    for normalized in documents:
        current = existing_by_external_id.get(normalized.external_id)
        if current and current.content_hash == normalized.content_hash:
            result.skipped_unchanged += 1
            continue

        service_name = normalized.metadata.get("service_name") or extract_service_from_path(normalized.path)
        try:
            raw_chunks, parse_error = _parse_normalized_document(normalized, service_name=service_name)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Failed parsing normalized document %s", normalized.path)
            raw_chunks, parse_error = [], f"parser raised {exc.__class__.__name__}: {exc}"

        if parse_error:
            result.errors.append(
                DocumentBatchError(
                    external_id=normalized.external_id,
                    path=normalized.path,
                    error=parse_error,
                )
            )
            continue
        if not raw_chunks:
            result.errors.append(
                DocumentBatchError(
                    external_id=normalized.external_id,
                    path=normalized.path,
                    error="no chunks parsed",
                )
            )
            continue

        document_row = current
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
            )
            db.add(document_row)
            await db.flush()
            result.created += 1
        else:
            document_row.title = PurePosixPath(normalized.path).name
            document_row.path = normalized.path
            document_row.doc_type = _doc_type_for_normalized_document(normalized)
            document_row.source_type = normalized.source_type
            document_row.checksum = normalized.content_hash
            document_row.content_hash = normalized.content_hash
            document_row.modified_at = normalized.modified_at
            document_row.size_bytes = normalized.size_bytes
            result.updated += 1

        document_operations.append(
            {
                "document": document_row,
                "replace_existing_chunks": current is not None,
            }
        )

        for raw_chunk in raw_chunks:
            sub_texts = (
                split_text_by_tokens(raw_chunk.text, settings.max_chunk_tokens)
                if count_tokens(raw_chunk.text) > settings.max_chunk_tokens
                else [raw_chunk.text]
            )
            for sub_text in sub_texts:
                chunk_payload = {
                    "project_id": project_id,
                    "document_id": document_row.id,
                    "source_id": source_id,
                    "chunk_type": raw_chunk.chunk_type,
                    "service_name": raw_chunk.service_name or service_name,
                    "endpoint": raw_chunk.endpoint,
                    "deploy_hash": raw_chunk.deploy_hash,
                    "timestamp_start": raw_chunk.timestamp_start,
                    "timestamp_end": raw_chunk.timestamp_end,
                    "section_title": raw_chunk.section_title,
                    "start_line": raw_chunk.start_line,
                    "end_line": raw_chunk.end_line,
                    "text": sub_text,
                    "token_count": count_tokens(sub_text),
                    "metadata_json": (raw_chunk.metadata or {})
                    | {
                        "source_type": raw_chunk.source_type,
                        "document_path": raw_chunk.document_path,
                    },
                }
                prepared_chunks.append(chunk_payload)
                prepared_embeddings_text.append(sub_text)

    if embed_fn and prepared_chunks:
        embeddings = embed_fn(prepared_embeddings_text)
        for chunk_payload, embedding in zip(prepared_chunks, embeddings):
            chunk_payload["embedding"] = embedding

    replaced_document_ids = {
        operation["document"].id
        for operation in document_operations
        if operation["replace_existing_chunks"]
    }
    if replaced_document_ids:
        await db.execute(delete(Chunk).where(Chunk.document_id.in_(replaced_document_ids)))

    for chunk_payload in prepared_chunks:
        db.add(Chunk(**chunk_payload))
    await db.flush()
    result.chunks_created = len(prepared_chunks)

    if prepared_chunks:
        touched_document_ids = list({payload["document_id"] for payload in prepared_chunks})
        await db.execute(
            update(Chunk)
            .where(Chunk.document_id.in_(touched_document_ids))
            .values(search_tsvector=func.to_tsvector("english", Chunk.text))
        )
        await db.flush()

    return result


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
    if source_type == "incident" or "incident" in lower_path or "postmortem" in lower_path:
        return parse_incident(document.content, document.path), None
    if source_type == "code" and suffix == ".py":
        return parse_python(document.content, document.path, service_name=service_name), None
    if suffix == ".py":
        return parse_python(document.content, document.path, service_name=service_name), None
    if suffix in {".yaml", ".yml"}:
        hinted_source_type = "api_doc" if source_type == "api_doc" else source_type
        return parse_markdown(document.content, document.path, source_type=hinted_source_type, service_name=service_name), None
    if suffix in {".md", ".txt", ".json"}:
        hinted_source_type = source_type or classify_source_type(document.path)
        return parse_markdown(document.content, document.path, source_type=hinted_source_type, service_name=service_name), None
    logger.info("No parser configured for normalized document %s", document.path)
    return [], "no parser configured"

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
from incidentops.ingestion.diagnostics import build_source_coverage
from incidentops.ingestion.failure_taxonomy import (
    FAILURE_CHUNK_LIMIT_EXCEEDED,
    FAILURE_EMBEDDING_FAILED,
    FAILURE_PARSER_EXCEPTION,
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
    result = BatchIngestResult(received=len(documents), embedding_backend=settings.embedding_model if embed_fn else None)
    if not documents:
        return result

    source_type_counts: Counter[str] = Counter()
    chunk_type_counts: Counter[str] = Counter()
    seen_external_ids: set[str] = set()
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
        "source_type_counts": result.source_type_counts,
        "chunk_type_counts": result.chunk_type_counts,
        "embedding_backend": result.embedding_backend,
    }
    return result


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
) -> dict[str, Any]:
    existing_result = await db.execute(
        select(Document).where(
            Document.project_id == project_id,
            Document.source_id == source_id,
            Document.external_id == normalized.external_id,
        )
    )
    current = existing_result.scalar_one_or_none()
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
                "deploy_hash": raw_chunk.deploy_hash or normalized.metadata.get("deploy_hash") or normalized.metadata.get("commit_sha"),
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

    if embed_fn and chunk_payloads:
        try:
            embeddings = embed_fn(embedding_texts)
        except Exception as exc:
            raise DocumentIndexingError("embedding_error", f"failed to embed document: {exc.__class__.__name__}") from exc
        if len(embeddings) != len(chunk_payloads):
            raise DocumentIndexingError("embedding_error", "embedding backend returned an unexpected result count")
        for chunk_payload, embedding in zip(chunk_payloads, embeddings):
            chunk_payload["embedding"] = embedding

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

        for chunk_payload in chunk_payloads:
            chunk_payload["document_id"] = document_row.id
            db.add(Chunk(**chunk_payload))
        await db.flush()
        await db.execute(
            update(Chunk)
            .where(Chunk.document_id == document_row.id)
            .values(search_tsvector=func.to_tsvector("english", Chunk.text))
        )
        await db.flush()

    return {
        "created": created,
        "updated": updated,
        "skipped_unchanged": 0,
        "chunks_created": len(chunk_payloads),
        "chunk_type_counts": dict(chunk_type_counts),
    }


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
    if suffix == ".go":
        return parse_go(document.content, document.path, service_name=service_name), None
    if suffix in {".js", ".ts", ".jsx", ".tsx"}:
        language = suffix.lstrip(".") if suffix != ".tsx" else "ts"
        return parse_jsts(document.content, document.path, service_name=service_name, language=language), None
    if suffix == ".java":
        return parse_java(document.content, document.path, service_name=service_name), None
    if suffix == ".proto":
        return parse_proto(document.content, document.path, service_name=service_name), None
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

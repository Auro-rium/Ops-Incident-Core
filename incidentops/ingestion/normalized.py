from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, Field

from incidentops.config.settings import Settings
from incidentops.ingestion.chunking.metadata import classify_source_type

SHA256_RE = re.compile(r"^(?:sha256:)?[a-fA-F0-9]{64}$")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

KNOWN_SOURCE_TYPES = {
    "logs",
    "code",
    "deploy",
    "incident",
    "runbook",
    "api_doc",
    "config",
    "unknown_text",
}

SOURCE_TYPE_ALIASES = {
    "log": "logs",
    "text": "unknown_text",
    "txt": "unknown_text",
    "markdown": "runbook",
    "md": "runbook",
    "docs": "runbook",
    "doc": "runbook",
    "postmortem": "incident",
    "incident_report": "incident",
    "incidents": "incident",
    "deploy_history": "deploy",
    "diff": "deploy",
    "patch": "deploy",
    "yaml": "config",
    "yml": "config",
    "json": "config",
}


class NormalizedDocument(BaseModel):
    external_id: str
    path: str
    source_type: str
    content: str
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    size_bytes: int | None = None
    modified_at: datetime | None = None


class DocumentBatchRequest(BaseModel):
    sync_id: str
    collector_id: str | None = None
    collector_version: str | None = None
    schema_version: str | None = None
    core_api_version: str | None = None
    documents: list[NormalizedDocument] = Field(default_factory=list)


class DocumentBatchError(BaseModel):
    external_id: str | None = None
    path: str | None = None
    code: str = "index_error"
    error: str
    message: str | None = None


class BatchIngestResult(BaseModel):
    received: int = 0
    created: int = 0
    updated: int = 0
    skipped_unchanged: int = 0
    skipped_invalid: int = 0
    chunks_created: int = 0
    errors: list[DocumentBatchError] = Field(default_factory=list)
    source_type_counts: dict[str, int] = Field(default_factory=dict)
    chunk_type_counts: dict[str, int] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    embedding_backend: str | None = None


class LocalIngestReadResult(BaseModel):
    documents: list[NormalizedDocument] = Field(default_factory=list)
    total_files_seen: int = 0
    files_ingested: int = 0
    files_skipped: int = 0
    skipped_files: list[dict[str, Any]] = Field(default_factory=list)
    parser_errors: list[dict[str, Any]] = Field(default_factory=list)
    source_type_counts: dict[str, int] = Field(default_factory=dict)


def validate_normalized_document(
    document: NormalizedDocument,
    settings: Settings,
    *,
    allow_absolute_path: bool = False,
) -> NormalizedDocument | DocumentBatchError:
    external_id = (document.external_id or "").strip()
    path = (document.path or "").strip().replace("\\", "/")
    source_type = normalize_source_type(document.source_type, path)

    if not external_id:
        return _error(document, "invalid_external_id", "external_id is required")
    if len(external_id) > settings.max_external_id_length:
        return _error(document, "external_id_too_long", "external_id exceeds configured length limit")
    if not _path_is_safe(external_id.replace("\\", "/"), allow_absolute=allow_absolute_path):
        return _error(document, "unsafe_external_id", "external_id is unsafe; use a source-relative identifier")
    if not path:
        return _error(document, "invalid_path", "path is required")
    if len(path) > settings.max_path_length:
        return _error(document, "path_too_long", "path exceeds configured length limit")
    if not _path_is_safe(path, allow_absolute=allow_absolute_path):
        return _error(document, "unsafe_path", "path is unsafe; use a source-relative path")
    if not document.content.strip():
        return _error(document, "empty_content", "document content is empty")

    content_bytes = len(document.content.encode("utf-8"))
    declared_size = document.size_bytes if document.size_bytes is not None else content_bytes
    if declared_size < 0:
        return _error(document, "invalid_size", "size_bytes must be non-negative")
    if content_bytes > settings.max_document_bytes or declared_size > settings.max_document_bytes:
        return _error(document, "document_too_large", "document exceeds configured byte limit")
    if not SHA256_RE.match((document.content_hash or "").strip()):
        return _error(document, "invalid_content_hash", "content_hash must be a sha256 hex digest")
    metadata_error = _validate_metadata(document.metadata, settings.max_metadata_bytes)
    if metadata_error:
        return _error(document, metadata_error[0], metadata_error[1])
    if _looks_binary(document.content):
        return _error(document, "binary_content", "document appears to contain binary data")

    return NormalizedDocument(
        external_id=external_id,
        path=path,
        source_type=source_type,
        content=document.content,
        content_hash=_normalize_content_hash(document.content_hash),
        metadata=document.metadata or {},
        size_bytes=document.size_bytes,
        modified_at=document.modified_at,
    )


def normalize_source_type(source_type: str | None, path: str) -> str:
    raw = (source_type or "").strip().lower()
    normalized = SOURCE_TYPE_ALIASES.get(raw, raw)
    if normalized in KNOWN_SOURCE_TYPES:
        return normalized
    inferred = classify_source_type(path)
    if inferred in {"logs", "code", "deploy", "incident", "api_doc", "runbook"}:
        return inferred
    suffix = PurePosixPath(path.lower()).suffix
    if suffix in {".json", ".yaml", ".yml"}:
        return "config"
    return "unknown_text"


def safe_json_size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _path_is_safe(path: str, *, allow_absolute: bool) -> bool:
    if "\x00" in path:
        return False
    pure = PurePosixPath(path)
    if pure.is_absolute() and not allow_absolute:
        return False
    return not any(part in {"..", ""} for part in pure.parts)


def _validate_metadata(metadata: Any, max_metadata_bytes: int) -> tuple[str, str] | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        return "invalid_metadata", "metadata must be an object"
    try:
        size = safe_json_size(metadata)
    except (TypeError, ValueError):
        return "invalid_metadata", "metadata must be JSON-serializable"
    if size > max_metadata_bytes:
        return "metadata_too_large", "metadata exceeds configured size limit"
    for key, value in metadata.items():
        if len(str(key)) > 256:
            return "metadata_too_large", "metadata key exceeds configured length limit"
        if isinstance(value, str) and len(value.encode("utf-8")) > 8192:
            return "metadata_too_large", "metadata value exceeds configured length limit"
    return None


def _looks_binary(content: str) -> bool:
    if "\x00" in content:
        return True
    sample = content[:4096]
    if not sample:
        return False
    control_chars = CONTROL_CHAR_RE.findall(sample)
    return len(control_chars) / max(len(sample), 1) > 0.05


def _normalize_content_hash(content_hash: str) -> str:
    value = content_hash.strip().lower()
    return value.removeprefix("sha256:")


def _error(document: NormalizedDocument, code: str, message: str) -> DocumentBatchError:
    return DocumentBatchError(
        external_id=(document.external_id or None),
        path=(document.path or None),
        code=code,
        error=message,
        message=message,
    )

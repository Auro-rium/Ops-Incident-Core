"""Stable failure reason taxonomy for ingestion diagnostics."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

FAILURE_UNSUPPORTED_EXTENSION = "unsupported_extension"
FAILURE_GENERATED_FILE = "generated_file"
FAILURE_VENDOR_FILE = "vendor_file"
FAILURE_OVERSIZED = "oversized"
FAILURE_EMPTY = "empty"
FAILURE_BINARY = "binary"
FAILURE_MALFORMED_CONTENT = "malformed_content"
FAILURE_PARSER_EXCEPTION = "parser_exception"
FAILURE_METADATA_INVALID = "metadata_invalid"
FAILURE_CHUNK_LIMIT_EXCEEDED = "chunk_limit_exceeded"
FAILURE_EMBEDDING_FAILED = "embedding_failed"
FAILURE_VECTOR_INDEX_FAILED = "vector_index_failed"
FAILURE_UNKNOWN = "unknown"

FAILURE_REASONS = {
    FAILURE_UNSUPPORTED_EXTENSION,
    FAILURE_GENERATED_FILE,
    FAILURE_VENDOR_FILE,
    FAILURE_OVERSIZED,
    FAILURE_EMPTY,
    FAILURE_BINARY,
    FAILURE_MALFORMED_CONTENT,
    FAILURE_PARSER_EXCEPTION,
    FAILURE_METADATA_INVALID,
    FAILURE_CHUNK_LIMIT_EXCEEDED,
    FAILURE_EMBEDDING_FAILED,
    FAILURE_VECTOR_INDEX_FAILED,
    FAILURE_UNKNOWN,
}

_ALIASES = {
    "unsupported_extension": FAILURE_UNSUPPORTED_EXTENSION,
    "generated_file": FAILURE_GENERATED_FILE,
    "vendor_file": FAILURE_VENDOR_FILE,
    "document_too_large": FAILURE_OVERSIZED,
    "external_id_too_long": FAILURE_OVERSIZED,
    "path_too_long": FAILURE_OVERSIZED,
    "empty_content": FAILURE_EMPTY,
    "no_chunks_parsed": FAILURE_EMPTY,
    "binary_content": FAILURE_BINARY,
    "malformed_content": FAILURE_MALFORMED_CONTENT,
    "parse_error": FAILURE_PARSER_EXCEPTION,
    "parser_exception": FAILURE_PARSER_EXCEPTION,
    "invalid_external_id": FAILURE_METADATA_INVALID,
    "unsafe_external_id": FAILURE_METADATA_INVALID,
    "duplicate_external_id": FAILURE_METADATA_INVALID,
    "invalid_path": FAILURE_METADATA_INVALID,
    "unsafe_path": FAILURE_METADATA_INVALID,
    "invalid_size": FAILURE_METADATA_INVALID,
    "invalid_content_hash": FAILURE_METADATA_INVALID,
    "invalid_metadata": FAILURE_METADATA_INVALID,
    "metadata_too_large": FAILURE_METADATA_INVALID,
    "too_many_chunks": FAILURE_CHUNK_LIMIT_EXCEEDED,
    "embedding_error": FAILURE_EMBEDDING_FAILED,
    "vector_index_failed": FAILURE_VECTOR_INDEX_FAILED,
    "index_error": FAILURE_UNKNOWN,
}


def normalize_failure_code(code: str | None) -> str:
    """Map legacy parser/indexer codes into stable public diagnostics."""
    raw = (code or "").strip().lower()
    if raw in FAILURE_REASONS:
        return raw
    return _ALIASES.get(raw, FAILURE_UNKNOWN)


def failure_reason_counts(codes: Iterable[str | None]) -> dict[str, int]:
    counts: Counter[str] = Counter(normalize_failure_code(code) for code in codes)
    return dict(sorted(counts.items()))

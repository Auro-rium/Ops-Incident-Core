from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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
    documents: list[NormalizedDocument] = Field(default_factory=list)


class DocumentBatchError(BaseModel):
    external_id: str | None = None
    path: str | None = None
    error: str


class BatchIngestResult(BaseModel):
    received: int = 0
    created: int = 0
    updated: int = 0
    skipped_unchanged: int = 0
    chunks_created: int = 0
    errors: list[DocumentBatchError] = Field(default_factory=list)


class LocalIngestReadResult(BaseModel):
    documents: list[NormalizedDocument] = Field(default_factory=list)
    total_files_seen: int = 0
    files_ingested: int = 0
    files_skipped: int = 0
    skipped_files: list[dict[str, Any]] = Field(default_factory=list)
    parser_errors: list[dict[str, Any]] = Field(default_factory=list)
    source_type_counts: dict[str, int] = Field(default_factory=dict)

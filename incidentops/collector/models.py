from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class DiscoveredFile:
    absolute_path: str
    relative_path: str
    size_bytes: int
    modified_at: datetime | None


@dataclass
class CollectorSummary:
    files_seen: int = 0
    files_skipped: int = 0
    documents_normalized: int = 0
    documents_synced: int = 0
    chunks_created: int = 0
    documents_created: int = 0
    documents_updated: int = 0
    documents_unchanged: int = 0
    bytes_uploaded: int = 0
    redaction_count: int = 0
    retry_attempted: int = 0
    retry_succeeded: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    source_type_counts: dict[str, int] = field(default_factory=dict)
    language_counts: dict[str, int] = field(default_factory=dict)
    hint_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.files_skipped += 1
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1

    def count(self, mapping: dict[str, int], value: str) -> None:
        mapping[value] = mapping.get(value, 0) + 1

    def diagnostics(self) -> dict[str, Any]:
        return {
            "total_files_seen": self.files_seen,
            "files_seen": self.files_seen,
            "files_skipped": self.files_skipped,
            "documents_normalized": self.documents_normalized,
            "documents_synced": self.documents_synced,
            "chunks_created": self.chunks_created,
            "documents_created": self.documents_created,
            "documents_updated": self.documents_updated,
            "documents_unchanged": self.documents_unchanged,
            "bytes_uploaded": self.bytes_uploaded,
            "redaction_count": self.redaction_count,
            "retry_attempted": self.retry_attempted,
            "retry_succeeded": self.retry_succeeded,
            "parser_error_count": 0,
            "parser_error_reasons": {},
            "chunk_discard_reasons": {},
            "embedding_failures": 0,
            "skipped_reasons": dict(sorted(self.skipped_reasons.items())),
            "source_type_counts": dict(sorted(self.source_type_counts.items())),
            "language_counts": dict(sorted(self.language_counts.items())),
            "hint_counts": dict(sorted(self.hint_counts.items())),
            "warnings": list(self.warnings),
        }

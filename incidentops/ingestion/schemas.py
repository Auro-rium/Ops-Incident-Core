"""
Ingestion schemas — canonical chunk representation flowing through the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawChunk:
    """
    Canonical chunk object produced by parsers.
    Every field here maps to a column or metadata key in the chunks table.
    """

    text: str
    chunk_type: str  # source-aware type such as go_function, log_time_window, or markdown_heading_section
    source_type: str  # code, logs, runbook, incident, deploy, api_doc

    # Document-level context
    document_path: str
    doc_type: str

    # Optional rich metadata
    service_name: str | None = None
    endpoint: str | None = None
    deploy_hash: str | None = None
    timestamp_start: datetime | None = None
    timestamp_end: datetime | None = None
    section_title: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    token_count: int | None = None

    # Freeform metadata
    metadata: dict = field(default_factory=dict)

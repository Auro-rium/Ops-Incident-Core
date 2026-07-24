"""
Log parser — parse structured log lines and chunk by time windows.
"""

from __future__ import annotations

import re
from datetime import datetime

from incidentops.ingestion.chunking.metadata import extract_service_from_path
from incidentops.ingestion.schemas import RawChunk

# Regex for common structured log format
LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s+"
    r"(?P<level>\w+)\s+"
    r"(?P<logger>[\w.]+)\s+"
    r"(?:\[trace_id=(?P<trace_id>[\w_]+)\]\s*)?"
    r"(?:\[deploy=(?P<deploy_hash>[\w]+)\]\s*)?"
    r"(?:(?P<method>GET|POST|PUT|DELETE|PATCH)\s+(?P<endpoint>/\S+)\s*)?"
    r"(?P<message>.*)"
)

WINDOW_SIZE = 30  # lines per chunk
ERROR_CLUSTER_MIN_LINES = 2
ERROR_CLUSTER_MAX_LINES = 24


def parse_logs(
    content: str,
    file_path: str,
    service_name: str | None = None,
    window_size: int = WINDOW_SIZE,
) -> list[RawChunk]:
    """
    Parse a log file into windowed chunks.
    Extracts timestamps, service, endpoint, deploy hash, trace IDs.
    """
    lines = content.strip().split("\n")
    if not lines:
        return []

    # Try to infer service from file path
    if service_name is None:
        service_name = _infer_service_from_path(file_path)

    chunks: list[RawChunk] = []
    for start_idx in range(0, len(lines), window_size):
        window = lines[start_idx : start_idx + window_size]
        chunk = _build_log_chunk(
            window,
            file_path=file_path,
            service_name=service_name,
            start_line=start_idx + 1,
            end_line=start_idx + len(window),
        )
        if chunk:
            chunks.append(chunk)

    # Windows preserve timeline context. Error clusters provide a second,
    # bounded retrieval unit when a runtime query needs the failure itself.
    chunks.extend(_error_clusters(lines, file_path=file_path, service_name=service_name))

    return chunks


def _build_log_chunk(
    lines: list[str],
    file_path: str,
    service_name: str | None,
    start_line: int,
    end_line: int,
) -> RawChunk | None:
    """Build a single log window chunk with extracted metadata."""
    text = "\n".join(lines)
    if not text.strip():
        return None

    timestamps: list[datetime] = []
    deploy_hashes: set[str] = set()
    trace_ids: set[str] = set()
    endpoints: set[str] = set()
    levels: set[str] = set()

    for line in lines:
        match = LOG_LINE_RE.match(line)
        if match:
            groups = match.groupdict()
            if groups.get("timestamp"):
                try:
                    ts = groups["timestamp"].rstrip("Z")
                    timestamps.append(datetime.fromisoformat(ts))
                except ValueError:
                    pass
            if groups.get("deploy_hash"):
                deploy_hashes.add(groups["deploy_hash"])
            if groups.get("trace_id"):
                trace_ids.add(groups["trace_id"])
            if groups.get("endpoint"):
                endpoints.add(groups["endpoint"])
            if groups.get("level"):
                levels.add(groups["level"].upper())

    # Pick the most relevant deploy hash (should be consistent in a window)
    deploy_hash = list(deploy_hashes)[0] if deploy_hashes else None
    endpoint = list(endpoints)[0] if len(endpoints) == 1 else None

    return RawChunk(
        text=text,
        chunk_type="log_window",
        source_type="logs",
        document_path=file_path,
        doc_type="log",
        service_name=service_name,
        endpoint=endpoint,
        deploy_hash=deploy_hash,
        timestamp_start=min(timestamps) if timestamps else None,
        timestamp_end=max(timestamps) if timestamps else None,
        start_line=start_line,
        end_line=end_line,
        metadata={
            "trace_ids": sorted(trace_ids),
            "levels": sorted(levels),
            "log_levels": sorted(levels),
            "endpoints": sorted(endpoints),
        },
    )


def _error_clusters(lines: list[str], *, file_path: str, service_name: str | None) -> list[RawChunk]:
    clusters: list[RawChunk] = []
    current: list[str] = []
    start_line = 1

    def flush(end_line: int) -> None:
        nonlocal current
        error_lines = [line for line in current if _is_error_line(line)]
        if len(error_lines) < ERROR_CLUSTER_MIN_LINES:
            current = []
            return
        bounded = current[:ERROR_CLUSTER_MAX_LINES]
        window = _build_log_chunk(
            bounded,
            file_path=file_path,
            service_name=service_name,
            start_line=start_line,
            end_line=start_line + len(bounded) - 1,
        )
        if window:
            window.chunk_type = "error_cluster"
            window.metadata["error_line_count"] = len(error_lines)
            window.metadata["cluster_end_line"] = end_line
            clusters.append(window)
        current = []

    for line_number, line in enumerate(lines, start=1):
        if _is_error_line(line):
            if not current:
                start_line = line_number
            current.append(line)
        elif current:
            # Keep one adjacent line for stack traces or a causal message.
            current.append(line)
            flush(line_number)
    if current:
        flush(len(lines))
    return clusters


def _is_error_line(line: str) -> bool:
    match = LOG_LINE_RE.match(line)
    if match and (match.group("level") or "").upper() in {"ERROR", "FATAL", "CRITICAL"}:
        return True
    return bool(re.search(r"\b(?:error|fatal|exception|panic|stack trace)\b", line, re.IGNORECASE))


def _infer_service_from_path(path: str) -> str | None:
    return extract_service_from_path(path)

"""
Incident report parser — chunks incident markdown by canonical sections.
"""

from __future__ import annotations

import re

from incidentops.ingestion.chunking.metadata import extract_service_from_path
from incidentops.ingestion.schemas import RawChunk

INCIDENT_SECTIONS = {
    "summary", "severity", "affected services", "symptoms",
    "timeline", "root cause", "fix", "lessons learned",
    "mitigation", "impact", "overview",
}


def parse_incident(content: str, file_path: str) -> list[RawChunk]:
    """
    Parse an incident report markdown into section-based chunks.
    Groups content by heading into incident-specific sections.
    """
    lines = content.split("\n")
    heading_re = re.compile(r"^(#{1,3})\s+(.+)$")

    sections: list[dict] = []
    current: dict | None = None

    for i, line in enumerate(lines, start=1):
        match = heading_re.match(line)
        if match:
            if current and current["lines"]:
                sections.append(current)
            current = {
                "title": match.group(2).strip(),
                "lines": [line],
                "start_line": i,
                "end_line": i,
            }
        elif current is not None:
            current["lines"].append(line)
            current["end_line"] = i
        else:
            # Content before first heading
            current = {
                "title": "Preamble",
                "lines": [line],
                "start_line": i,
                "end_line": i,
            }

    if current and current["lines"]:
        sections.append(current)

    # Extract service name from file path or content
    service_name = _extract_service(content, file_path)

    chunks: list[RawChunk] = []
    for section in sections:
        text = "\n".join(section["lines"]).strip()
        if not text:
            continue

        chunks.append(
            RawChunk(
                text=text,
                chunk_type="incident_section",
                source_type="incident",
                document_path=file_path,
                doc_type="incident",
                service_name=service_name,
                section_title=section["title"],
                start_line=section["start_line"],
                end_line=section["end_line"],
            )
        )

    return chunks


def _extract_service(content: str, path: str) -> str | None:
    title_match = re.search(r"^#\s+([A-Za-z][A-Za-z0-9_-]{2,40})", content, re.MULTILINE)
    if title_match and title_match.group(1).lower() not in {"incident", "postmortem"}:
        return title_match.group(1).lower()
    match = re.search(r"\bservice[:\s-]+([a-z][a-z0-9_-]{2,40})\b", content, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    service = extract_service_from_path(path)
    return service if service not in {"incident", "postmortem"} else None

"""
Incident report parser — chunks incident markdown by canonical sections.
"""

from __future__ import annotations

import re

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
                chunk_type=_chunk_type(section["title"]),
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


def _chunk_type(title: str) -> str:
    normalized = title.lower()
    if any(term in normalized for term in ("root cause", "cause")):
        return "incident_root_cause"
    if "timeline" in normalized:
        return "incident_timeline"
    if any(term in normalized for term in ("fix", "mitigation", "action", "lessons")):
        return "incident_action"
    if any(term in normalized for term in ("summary", "symptom", "impact", "severity", "overview")):
        return "incident_symptom"
    return "incident_section"


FRONT_MATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
SERVICE_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?service(?:[_ ]name)?(?:\*\*)?\s*:\s*(?:\*\*)?\s*[`'\"]?([A-Za-z][A-Za-z0-9_-]{1,62})",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_service(content: str, path: str) -> str | None:
    """Only explicit service declarations; the indexer supplies path/source fallbacks."""
    front_matter = FRONT_MATTER_RE.match(content)
    if front_matter:
        match = SERVICE_LINE_RE.search(front_matter.group("body"))
        if match:
            return match.group(1).lower()
    match = SERVICE_LINE_RE.search(content)
    return match.group(1).lower() if match else None

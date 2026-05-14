"""
Citation builder — generate [N] labels and format citations for evidence items.
"""

from __future__ import annotations


def build_citations(evidence: list[dict]) -> list[dict]:
    """
    Assign citation labels [1], [2], ... to evidence items.
    Returns enriched evidence with 'citation' field.
    """
    seen_paths: set[str] = set()
    citations: list[dict] = []

    for i, item in enumerate(evidence):
        label = f"[{i + 1}]"
        doc_path = item.get("document_path", "unknown")
        lines = _format_lines(item.get("start_line"), item.get("end_line"))

        citation = {
            "label": label,
            "path": doc_path,
            "lines": lines,
            "chunk_id": item.get("chunk_id"),
            "source_type": item.get("source_type"),
        }

        item["citation"] = citation
        citations.append(citation)

    return citations


def format_citation_string(citation: dict) -> str:
    """Format a citation as a readable string: [1] path:lines"""
    label = citation.get("label", "[?]")
    path = citation.get("path", "unknown")
    lines = citation.get("lines", "")
    if lines:
        return f"{label} {path}:{lines}"
    return f"{label} {path}"


def format_citations_block(evidence: list[dict]) -> str:
    """Format all citations as a block for LLM context."""
    lines: list[str] = []
    for item in evidence:
        cit = item.get("citation")
        if cit:
            lines.append(format_citation_string(cit))
    return "\n".join(lines)


def _format_lines(start: int | None, end: int | None) -> str:
    if start is not None and end is not None:
        if start == end:
            return str(start)
        return f"{start}-{end}"
    if start is not None:
        return str(start)
    return ""

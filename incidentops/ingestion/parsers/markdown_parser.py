"""
Markdown parser — splits markdown files by heading sections.
"""

from __future__ import annotations

import re

from incidentops.ingestion.schemas import RawChunk


def parse_markdown(
    content: str,
    file_path: str,
    source_type: str = "runbook",
    service_name: str | None = None,
) -> list[RawChunk]:
    """
    Parse a markdown file into chunks split by headings.
    Each heading section becomes one chunk.
    """
    lines = content.split("\n")
    chunks: list[RawChunk] = []
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")

    current_title: str | None = None
    current_lines: list[str] = []
    current_start: int = 1

    for i, line in enumerate(lines, start=1):
        match = heading_pattern.match(line)
        if match and current_lines:
            # Flush the previous section
            text = "\n".join(current_lines).strip()
            if text:
                chunks.append(
                    RawChunk(
                        text=text,
                        chunk_type=_chunk_type(current_title, text),
                        source_type=source_type,
                        document_path=file_path,
                        doc_type="markdown",
                        service_name=service_name,
                        section_title=current_title,
                        start_line=current_start,
                        end_line=i - 1,
                    )
                )
            current_title = match.group(2).strip()
            current_lines = [line]
            current_start = i
        else:
            if match:
                current_title = match.group(2).strip()
                current_start = i
            current_lines.append(line)

    # Flush last section
    if current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            chunks.append(
                RawChunk(
                    text=text,
                    chunk_type=_chunk_type(current_title, text),
                    source_type=source_type,
                    document_path=file_path,
                    doc_type="markdown",
                    service_name=service_name,
                    section_title=current_title,
                    start_line=current_start,
                    end_line=len(lines),
                )
            )

    return chunks


def _chunk_type(title: str | None, text: str) -> str:
    normalized_title = (title or "").lower()
    if re.search(r"^\s*\|.+\|\s*$\n^\s*\|\s*:?-{3,}", text, re.MULTILINE):
        return "markdown_table"
    if any(term in normalized_title for term in ("procedure", "runbook", "how to", "steps", "playbook")) or re.search(
        r"^\s*\d+[.)]\s+", text, re.MULTILINE
    ):
        return "markdown_procedure"
    if any(term in normalized_title for term in ("faq", "questions", "q&a")):
        return "markdown_faq"
    return "markdown_heading_section"

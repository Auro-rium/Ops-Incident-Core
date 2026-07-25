from __future__ import annotations

import re

_GO_BOUNDARY = re.compile(r"^\s*(?:func\s+(?:\([^)]*\)\s*)?\w+\s*\(|type\s+\w+\s+(?:struct|interface))", re.MULTILINE)
_PROTO_BOUNDARY = re.compile(r"^\s*(?:service|rpc|message|enum)\s+([A-Za-z_]\w*)", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def build(path: str, content: str, source_type: str) -> list[dict]:
    lines = content.splitlines()
    if source_type == "code" and path.endswith(".go"):
        return _boundaries(path, lines, _GO_BOUNDARY, "go_symbol")
    if source_type == "api_doc" and path.endswith(".proto"):
        return _boundaries(path, lines, _PROTO_BOUNDARY, "proto_symbol")
    if path.endswith(".md"):
        return _boundaries(path, lines, _HEADING, "markdown_section")
    return [{"type": "document", "start_line": 1, "end_line": max(len(lines), 1), "priority": "low"}]


def _boundaries(path: str, lines: list[str], pattern: re.Pattern[str], kind: str) -> list[dict]:
    boundaries = [index for index, line in enumerate(lines, 1) if pattern.match(line)]
    if not boundaries:
        return [{"type": "document", "start_line": 1, "end_line": max(len(lines), 1), "priority": "low"}]
    result: list[dict] = []
    for position, start_line in enumerate(boundaries):
        end_line = boundaries[position + 1] - 1 if position + 1 < len(boundaries) else len(lines)
        result.append({"type": kind, "start_line": start_line, "end_line": max(end_line, start_line), "priority": "high"})
    return result[:500]

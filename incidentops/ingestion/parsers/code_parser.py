"""
Python code parser — extracts functions and classes using AST.
"""

from __future__ import annotations

import ast
import re

from incidentops.ingestion.schemas import RawChunk

GO_DECL_RE = re.compile(r"^(func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)|type\s+([A-Za-z_]\w*)\s+(?:struct|interface|func|\w+))\b")
PROTO_DECL_RE = re.compile(r"^\s*(service|message|enum)\s+([A-Za-z_]\w*)\s*\{?")


def parse_python(
    content: str,
    file_path: str,
    service_name: str | None = None,
) -> list[RawChunk]:
    """
    Parse a Python file into chunks — one per function/class.
    Falls back to whole-file chunk if AST parsing fails.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        # Unparseable — return whole file as one chunk
        return [
            RawChunk(
                text=content,
                chunk_type="function",
                source_type="code",
                document_path=file_path,
                doc_type="code",
                service_name=service_name,
                start_line=1,
                end_line=content.count("\n") + 1,
            )
        ]

    lines = content.split("\n")
    chunks: list[RawChunk] = []

    # Collect module-level docstring / imports as a preamble chunk
    first_def_line = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if first_def_line is None or node.lineno < first_def_line:
                first_def_line = node.lineno

    if first_def_line and first_def_line > 1:
        preamble = "\n".join(lines[: first_def_line - 1]).strip()
        if preamble:
            chunks.append(
                RawChunk(
                    text=preamble,
                    chunk_type="function",
                    source_type="code",
                    document_path=file_path,
                    doc_type="code",
                    service_name=service_name,
                    section_title="module preamble",
                    start_line=1,
                    end_line=first_def_line - 1,
                )
            )

    # Extract each top-level function/class
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = node.end_lineno or start
            text = "\n".join(lines[start - 1 : end])

            chunk_type = "function"
            section_title = f"{'class' if isinstance(node, ast.ClassDef) else 'def'} {node.name}"

            chunks.append(
                RawChunk(
                    text=text,
                    chunk_type=chunk_type,
                    source_type="code",
                    document_path=file_path,
                    doc_type="code",
                    service_name=service_name,
                    section_title=section_title,
                    start_line=start,
                    end_line=end,
                )
            )

    # If no functions/classes found, return whole file
    if not chunks:
        chunks.append(
            RawChunk(
                text=content,
                chunk_type="function",
                source_type="code",
                document_path=file_path,
                doc_type="code",
                service_name=service_name,
                start_line=1,
                end_line=len(lines),
            )
        )

    return chunks


def parse_go(
    content: str,
    file_path: str,
    service_name: str | None = None,
) -> list[RawChunk]:
    """Parse Go source into package/import and top-level declaration chunks."""
    return _parse_declaration_language(
        content,
        file_path,
        service_name=service_name,
        source_type="code",
        fallback_chunk_type="code_file",
        declaration_re=GO_DECL_RE,
        language="go",
    )


def parse_proto(
    content: str,
    file_path: str,
    service_name: str | None = None,
) -> list[RawChunk]:
    """Parse protobuf files into service/message/enum chunks."""
    return _parse_declaration_language(
        content,
        file_path,
        service_name=service_name,
        source_type="api_doc",
        fallback_chunk_type="api_endpoint",
        declaration_re=PROTO_DECL_RE,
        language="proto",
    )


def _parse_declaration_language(
    content: str,
    file_path: str,
    *,
    service_name: str | None,
    source_type: str,
    fallback_chunk_type: str,
    declaration_re: re.Pattern[str],
    language: str,
) -> list[RawChunk]:
    lines = content.splitlines()
    if not lines:
        return []

    declarations: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines, start=1):
        match = declaration_re.match(line.strip())
        if not match:
            continue
        groups = [group for group in match.groups() if group]
        symbol = groups[-1] if groups else line.strip().split()[0]
        kind = "service" if language == "proto" and line.strip().startswith("service ") else "declaration"
        if language == "go":
            kind = "function" if line.strip().startswith("func ") else "type"
        elif language == "proto":
            kind = line.strip().split()[0]
        declarations.append((index, kind, symbol))

    chunks: list[RawChunk] = []
    if declarations and declarations[0][0] > 1:
        preamble = "\n".join(lines[: declarations[0][0] - 1]).strip()
        if preamble:
            chunks.append(
                RawChunk(
                    text=preamble,
                    chunk_type=f"{language}_preamble",
                    source_type=source_type,
                    document_path=file_path,
                    doc_type="code" if source_type == "code" else "api_doc",
                    service_name=service_name,
                    section_title="module preamble",
                    start_line=1,
                    end_line=declarations[0][0] - 1,
                    metadata={"language": language, "kind": "preamble"},
                )
            )

    for idx, (start_line, kind, symbol) in enumerate(declarations):
        end_line = declarations[idx + 1][0] - 1 if idx + 1 < len(declarations) else len(lines)
        text = "\n".join(lines[start_line - 1 : end_line]).strip()
        if not text:
            continue
        chunks.append(
            RawChunk(
                text=text,
                chunk_type="function" if source_type == "code" else "api_endpoint",
                source_type=source_type,
                document_path=file_path,
                doc_type="code" if source_type == "code" else "api_doc",
                service_name=service_name,
                section_title=f"{kind} {symbol}",
                start_line=start_line,
                end_line=end_line,
                metadata={"language": language, "symbol": symbol, "kind": kind},
            )
        )

    if chunks:
        return chunks

    return [
        RawChunk(
            text=content,
            chunk_type=fallback_chunk_type,
            source_type=source_type,
            document_path=file_path,
            doc_type="code" if source_type == "code" else "api_doc",
            service_name=service_name,
            section_title=file_path.rsplit("/", 1)[-1],
            start_line=1,
            end_line=len(lines),
            metadata={"language": language, "kind": "file"},
        )
    ]

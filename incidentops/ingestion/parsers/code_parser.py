"""
Python code parser — extracts functions and classes using AST.
"""

from __future__ import annotations

import ast

from incidentops.ingestion.schemas import RawChunk


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

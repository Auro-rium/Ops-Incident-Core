"""Code and structured config parsers used by the central indexer."""

from __future__ import annotations

import ast
from configparser import ConfigParser
import json
import re
import tomllib

import yaml

from incidentops.ingestion.schemas import RawChunk

GO_DECL_RE = re.compile(r"^(func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)|type\s+([A-Za-z_]\w*)\s+(?:struct|interface|func|\w+))\b")
GO_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_]\w*)\b", re.MULTILINE)
GO_FUNC_RE = re.compile(r"^\s*func\s+(?P<receiver>\([^)]*\)\s*)?(?P<name>[A-Za-z_]\w*)\s*\(")
GO_TYPE_RE = re.compile(r"^\s*type\s+(?P<name>[A-Za-z_]\w*)\s+(?P<kind>struct|interface|func|\w+)\b")
PROTO_DECL_RE = re.compile(r"^\s*(service|message|enum)\s+([A-Za-z_]\w*)\s*\{?")
PROTO_RPC_RE = re.compile(r"^\s*rpc\s+([A-Za-z_]\w*)\s*\(")
PROTO_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;", re.MULTILINE)
JS_DECL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:(class)\s+([A-Za-z_]\w*)|(async\s+)?function\s+([A-Za-z_]\w*)|const\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(|let\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(|var\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\()"
)
JAVA_DECL_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*(class|interface|enum|record|void|[A-Za-z_][\w<>\[\]]+)\s+([A-Za-z_]\w*)\s*(?:\(|\{)"
)


def parse_python(content: str, file_path: str, service_name: str | None = None) -> list[RawChunk]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return [
            RawChunk(
                text=content,
                chunk_type="code_file",
                source_type="code",
                document_path=file_path,
                doc_type="code",
                service_name=service_name,
                start_line=1,
                end_line=content.count("\n") + 1,
                metadata={"language": "py", "kind": "file"},
            )
        ]

    lines = content.split("\n")
    chunks: list[RawChunk] = []

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
                    chunk_type="module",
                    source_type="code",
                    document_path=file_path,
                    doc_type="code",
                    service_name=service_name,
                    section_title="module preamble",
                    start_line=1,
                    end_line=first_def_line - 1,
                    metadata={"language": "py", "kind": "preamble"},
                )
            )

    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = node.lineno
        end = node.end_lineno or start
        text = "\n".join(lines[start - 1 : end])
        is_class = isinstance(node, ast.ClassDef)
        chunks.append(
            RawChunk(
                text=text,
                chunk_type="class" if is_class else "function",
                source_type="code",
                document_path=file_path,
                doc_type="code",
                service_name=service_name,
                section_title=f"{'class' if is_class else 'def'} {node.name}",
                start_line=start,
                end_line=end,
                metadata={"language": "py", "symbol": node.name, "kind": "class" if is_class else "function"},
            )
        )

    if not chunks:
        chunks.append(
            RawChunk(
                text=content,
                chunk_type="code_file",
                source_type="code",
                document_path=file_path,
                doc_type="code",
                service_name=service_name,
                start_line=1,
                end_line=len(lines),
                metadata={"language": "py", "kind": "file"},
            )
        )

    return chunks


def parse_go(content: str, file_path: str, service_name: str | None = None) -> list[RawChunk]:
    lines = content.splitlines()
    if not lines:
        return []

    package_name = _match_first(GO_PACKAGE_RE, content)
    declarations: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        function_match = GO_FUNC_RE.match(stripped)
        if function_match:
            kind = "method" if function_match.group("receiver") else "function"
            declarations.append((index, kind, function_match.group("name")))
            continue
        type_match = GO_TYPE_RE.match(stripped)
        if type_match:
            declarations.append((index, "type", type_match.group("name")))

    chunks = _declaration_chunks(
        content,
        file_path,
        declarations,
        service_name=service_name,
        source_type="code",
        doc_type="code",
        preamble_chunk_type="go_module",
        fallback_chunk_type="code_file",
        language="go",
        chunk_type_for_kind={"function": "go_function", "method": "go_method", "type": "go_type"},
        package_name=package_name,
    )
    return chunks


def parse_proto(content: str, file_path: str, service_name: str | None = None) -> list[RawChunk]:
    lines = content.splitlines()
    if not lines:
        return []

    package_name = _match_first(PROTO_PACKAGE_RE, content)
    declarations: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines, start=1):
        decl_match = PROTO_DECL_RE.match(line)
        if decl_match:
            declarations.append((index, decl_match.group(1), decl_match.group(2)))
            continue
        rpc_match = PROTO_RPC_RE.match(line)
        if rpc_match:
            declarations.append((index, "rpc", rpc_match.group(1)))

    chunks = _declaration_chunks(
        content,
        file_path,
        declarations,
        service_name=service_name,
        source_type="api_doc",
        doc_type="api_doc",
        preamble_chunk_type="proto_preamble",
        fallback_chunk_type="api_endpoint",
        language="proto",
        chunk_type_for_kind={
            "service": "proto_service",
            "rpc": "proto_rpc",
            "message": "proto_message",
            "enum": "proto_enum",
        },
        package_name=package_name,
    )
    return chunks


def parse_jsts(
    content: str,
    file_path: str,
    *,
    service_name: str | None = None,
    language: str,
) -> list[RawChunk]:
    return _parse_regex_language(
        content,
        file_path,
        service_name=service_name,
        source_type="code",
        fallback_chunk_type="code_file",
        declaration_re=JS_DECL_RE,
        language=language,
    )


def parse_java(content: str, file_path: str, *, service_name: str | None = None) -> list[RawChunk]:
    return _parse_regex_language(
        content,
        file_path,
        service_name=service_name,
        source_type="code",
        fallback_chunk_type="code_file",
        declaration_re=JAVA_DECL_RE,
        language="java",
    )


def parse_structured_config(
    content: str,
    file_path: str,
    *,
    source_type: str,
    service_name: str | None = None,
) -> list[RawChunk]:
    lower = file_path.lower()
    if source_type == "api_doc":
        api_chunks = _parse_openapi_like(content, file_path, service_name=service_name)
        if api_chunks:
            return api_chunks

    data = _load_structured_data(content, lower)
    lines = content.splitlines() or [content]
    if isinstance(data, dict) and data:
        chunks: list[RawChunk] = []
        for key, value in list(data.items())[:50]:
            text = _render_structured_section(key, value)
            if not text.strip():
                continue
            chunks.append(
                RawChunk(
                    text=text,
                    chunk_type="config_section",
                    source_type=source_type,
                    document_path=file_path,
                    doc_type="api_doc" if source_type == "api_doc" else "markdown",
                    service_name=service_name,
                    section_title=f"config {key}",
                    start_line=1,
                    end_line=len(lines),
                    metadata={"language": _language_from_path(lower), "config_key": key, "kind": "config"},
                )
            )
        if chunks:
            return chunks

    return [
        RawChunk(
            text=content,
            chunk_type="config_section",
            source_type=source_type,
            document_path=file_path,
            doc_type="api_doc" if source_type == "api_doc" else "markdown",
            service_name=service_name,
            section_title=file_path.rsplit("/", 1)[-1],
            start_line=1,
            end_line=len(lines),
            metadata={"language": _language_from_path(lower), "kind": "config_file"},
        )
    ]


def _declaration_chunks(
    content: str,
    file_path: str,
    declarations: list[tuple[int, str, str]],
    *,
    service_name: str | None,
    source_type: str,
    doc_type: str,
    preamble_chunk_type: str,
    fallback_chunk_type: str,
    language: str,
    chunk_type_for_kind: dict[str, str],
    package_name: str | None = None,
) -> list[RawChunk]:
    lines = content.splitlines()
    chunks: list[RawChunk] = []
    if declarations and declarations[0][0] > 1:
        preamble = "\n".join(lines[: declarations[0][0] - 1]).strip()
        if preamble:
            chunks.append(
                RawChunk(
                    text=preamble,
                    chunk_type=preamble_chunk_type,
                    source_type=source_type,
                    document_path=file_path,
                    doc_type=doc_type,
                    service_name=service_name,
                    section_title="module preamble",
                    start_line=1,
                    end_line=declarations[0][0] - 1,
                    metadata={
                        "language": language,
                        "kind": "preamble",
                        **({"package_name": package_name} if package_name else {}),
                    },
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
                chunk_type=chunk_type_for_kind.get(kind, fallback_chunk_type),
                source_type=source_type,
                document_path=file_path,
                doc_type=doc_type,
                service_name=service_name,
                section_title=f"{kind} {symbol}",
                start_line=start_line,
                end_line=end_line,
                metadata={
                    "language": language,
                    "symbol": symbol,
                    "symbol_name": symbol,
                    "kind": kind,
                    **({"package_name": package_name} if package_name else {}),
                },
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
            doc_type=doc_type,
            service_name=service_name,
            section_title=file_path.rsplit("/", 1)[-1],
            start_line=1,
            end_line=len(lines),
            metadata={
                "language": language,
                "kind": "file",
                **({"package_name": package_name} if package_name else {}),
            },
        )
    ]


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
                    chunk_type="module" if source_type == "code" else f"{language}_preamble",
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
        chunk_type = "function" if source_type == "code" else _proto_chunk_type(kind)
        if source_type == "code" and kind == "type":
            chunk_type = "class"
        chunks.append(
            RawChunk(
                text=text,
                chunk_type=chunk_type,
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


def _match_first(pattern: re.Pattern[str], content: str) -> str | None:
    match = pattern.search(content)
    return match.group(1) if match else None


def _parse_regex_language(
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
        match = declaration_re.match(line)
        if not match:
            continue
        groups = [group for group in match.groups() if group]
        if not groups:
            continue
        kind, symbol = _resolve_regex_declaration(groups, language)
        declarations.append((index, kind, symbol))

    if not declarations:
        return [
            RawChunk(
                text=content,
                chunk_type=fallback_chunk_type,
                source_type=source_type,
                document_path=file_path,
                doc_type="code",
                service_name=service_name,
                section_title=file_path.rsplit("/", 1)[-1],
                start_line=1,
                end_line=len(lines),
                metadata={"language": language, "kind": "file"},
            )
        ]

    chunks: list[RawChunk] = []
    first_line = declarations[0][0]
    if first_line > 1:
        preamble = "\n".join(lines[: first_line - 1]).strip()
        if preamble:
            chunks.append(
                RawChunk(
                    text=preamble,
                    chunk_type="module",
                    source_type=source_type,
                    document_path=file_path,
                    doc_type="code",
                    service_name=service_name,
                    section_title="module preamble",
                    start_line=1,
                    end_line=first_line - 1,
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
                chunk_type="class" if kind in {"class", "interface", "enum", "record"} else "function",
                source_type=source_type,
                document_path=file_path,
                doc_type="code",
                service_name=service_name,
                section_title=f"{kind} {symbol}",
                start_line=start_line,
                end_line=end_line,
                metadata={"language": language, "symbol": symbol, "kind": kind},
            )
        )
    return chunks


def _resolve_regex_declaration(groups: list[str], language: str) -> tuple[str, str]:
    if language in {"js", "ts"}:
        if groups[0] == "class":
            return "class", groups[1]
        for symbol in groups[3:]:
            if symbol:
                return "function", symbol
        return "function", groups[-1]
    kind = groups[0].lower()
    symbol = groups[1]
    if kind not in {"class", "interface", "enum", "record"}:
        kind = "function"
    return kind, symbol


def _parse_openapi_like(content: str, file_path: str, *, service_name: str | None) -> list[RawChunk]:
    data = _load_structured_data(content, file_path.lower())
    if not isinstance(data, dict):
        return []
    paths = data.get("paths")
    if not isinstance(paths, dict) or not paths:
        return []
    lines = content.splitlines() or [content]
    chunks: list[RawChunk] = []
    for endpoint, value in list(paths.items())[:100]:
        methods: list[str] = []
        if isinstance(value, dict):
            methods = [method.upper() for method in value.keys() if method.lower() in {"get", "post", "put", "patch", "delete"}]
        label = ", ".join(methods) + f" {endpoint}" if methods else str(endpoint)
        chunks.append(
            RawChunk(
                text=_render_structured_section(str(endpoint), value),
                chunk_type="api_endpoint",
                source_type="api_doc",
                document_path=file_path,
                doc_type="api_doc",
                service_name=service_name,
                section_title=label,
                endpoint=label if methods else str(endpoint),
                start_line=1,
                end_line=len(lines),
                metadata={"language": _language_from_path(file_path.lower()), "endpoint": str(endpoint), "methods": methods, "kind": "openapi_path"},
            )
        )
    return chunks


def _load_structured_data(content: str, lower_path: str):
    try:
        if lower_path.endswith(".json"):
            return json.loads(content)
        if lower_path.endswith(".toml"):
            return tomllib.loads(content)
        if lower_path.endswith(".ini"):
            parser = ConfigParser()
            parser.read_string(content)
            return {section: dict(parser.items(section)) for section in parser.sections()}
        if lower_path.endswith((".yaml", ".yml")):
            return yaml.safe_load(content)
    except Exception:
        return None
    return None


def _render_structured_section(key: str, value) -> str:
    try:
        rendered = json.dumps(value, indent=2, sort_keys=True)
    except TypeError:
        rendered = str(value)
    return f"{key}\n{rendered}".strip()


def _language_from_path(lower_path: str) -> str:
    return lower_path.rsplit(".", 1)[-1] if "." in lower_path else "text"


def _proto_chunk_type(kind: str) -> str:
    if kind == "service":
        return "proto_service"
    if kind == "message":
        return "proto_message"
    return "api_endpoint"

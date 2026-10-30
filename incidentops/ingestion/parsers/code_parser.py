"""Code and structured config parsers used by the central indexer."""

from __future__ import annotations

import ast
from configparser import ConfigParser
import json
import re
import tomllib

import yaml

from incidentops.ingestion.schemas import RawChunk

GO_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_]\w*)\b", re.MULTILINE)
GO_FUNC_RE = re.compile(r"^\s*func\s+(?P<receiver>\([^)]*\)\s*)?(?P<name>[A-Za-z_]\w*)\s*\(")
GO_TYPE_RE = re.compile(r"^\s*type\s+(?P<name>[A-Za-z_]\w*)\s+(?P<kind>struct|interface|func|\w+)\b")
PROTO_DECL_RE = re.compile(r"^\s*(service|message|enum)\s+([A-Za-z_]\w*)\s*\{?")
PROTO_RPC_RE = re.compile(r"^\s*rpc\s+([A-Za-z_]\w*)\s*\(")
PROTO_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;", re.MULTILINE)
JS_DECL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:(class|interface|type|enum)\s+([A-Za-z_]\w*)|(async\s+)?function\s+([A-Za-z_]\w*)|const\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(|let\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(|var\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\()"
)
MAX_CONFIG_KEY_CHUNKS = 50
JAVA_DECL_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*"
    r"(?!(?:return|if|else|for|while|switch|catch|new|throw)\b)"
    r"(class|interface|enum|record|void|[A-Za-z_][\w<>\[\]]+)\s+([A-Za-z_]\w*)\s*(?:\(|\{)"
)


def parse_python(content: str, file_path: str, service_name: str | None = None) -> list[RawChunk]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return [
            RawChunk(
                text=content,
                chunk_type="python_fallback",
                source_type="code",
                document_path=file_path,
                doc_type="code",
                service_name=service_name,
                start_line=1,
                end_line=content.count("\n") + 1,
                metadata={"language": "python", "kind": "file", "fallback_reason": "syntax_error"},
            )
        ]

    lines = content.split("\n")
    chunks: list[RawChunk] = []

    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    if not definitions:
        return [_python_fallback_chunk(content, file_path, service_name, len(lines))]

    spans: list[tuple[int, int, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]] = []
    for node in definitions:
        # Decorators belong to the definition they wrap.
        start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
        spans.append((start, node.end_lineno or node.lineno, node))

    cursor = 1
    for start, end, node in spans:
        if start > cursor:
            _append_python_module_chunk(chunks, lines, cursor, start - 1, file_path, service_name)
        text = "\n".join(lines[start - 1 : end])
        is_class = isinstance(node, ast.ClassDef)
        chunks.append(
            RawChunk(
                text=text,
                chunk_type="python_class" if is_class else "python_function",
                source_type="code",
                document_path=file_path,
                doc_type="code",
                service_name=service_name,
                section_title=f"{'class' if is_class else 'def'} {node.name}",
                start_line=start,
                end_line=end,
                metadata={
                    "language": "python",
                    "symbol": node.name,
                    "symbol_name": node.name,
                    "kind": "class" if is_class else "function",
                },
            )
        )
        cursor = end + 1
    if cursor <= len(lines):
        _append_python_module_chunk(chunks, lines, cursor, len(lines), file_path, service_name)

    return chunks


def _append_python_module_chunk(
    chunks: list[RawChunk],
    lines: list[str],
    start: int,
    end: int,
    file_path: str,
    service_name: str | None,
) -> None:
    """Emit module-level code between or around definitions, trimmed to non-blank lines."""
    while start <= end and not lines[start - 1].strip():
        start += 1
    while end >= start and not lines[end - 1].strip():
        end -= 1
    if start > end:
        return
    is_preamble = not chunks
    chunks.append(
        RawChunk(
            text="\n".join(lines[start - 1 : end]),
            chunk_type="python_module",
            source_type="code",
            document_path=file_path,
            doc_type="code",
            service_name=service_name,
            section_title="module preamble" if is_preamble else f"module code L{start}-{end}",
            start_line=start,
            end_line=end,
            metadata={"language": "python", "kind": "preamble" if is_preamble else "module_code"},
        )
    )


def _python_fallback_chunk(content: str, file_path: str, service_name: str | None, line_count: int) -> RawChunk:
    return RawChunk(
        text=content,
        chunk_type="python_fallback",
        source_type="code",
        document_path=file_path,
        doc_type="code",
        service_name=service_name,
        start_line=1,
        end_line=line_count,
        metadata={"language": "python", "kind": "file", "fallback_reason": "no_declarations"},
    )


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
            declarations.append((index, type_match.group("kind"), type_match.group("name")))

    chunks = _declaration_chunks(
        content,
        file_path,
        declarations,
        service_name=service_name,
        source_type="code",
        doc_type="code",
        preamble_chunk_type="go_module",
        fallback_chunk_type="go_fallback",
        language="go",
        chunk_type_for_kind={
            "function": "go_function",
            "method": "go_method",
            "struct": "go_struct",
            "interface": "go_interface",
            "func": "go_type",
        },
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
        fallback_chunk_type="proto_fallback",
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
    prefix = "ts" if language in {"ts", "tsx"} else "js"
    return _parse_regex_language(
        content,
        file_path,
        service_name=service_name,
        source_type="code",
        fallback_chunk_type=f"{prefix}_fallback",
        declaration_re=JS_DECL_RE,
        language=language,
        module_chunk_type=f"{prefix}_module",
        chunk_type_for_kind={
            "class": f"{prefix}_class",
            "interface": f"{prefix}_interface",
            "type": f"{prefix}_interface",
            "enum": f"{prefix}_class",
            "function": f"{prefix}_function",
        },
    )


def parse_java(content: str, file_path: str, *, service_name: str | None = None) -> list[RawChunk]:
    return _parse_regex_language(
        content,
        file_path,
        service_name=service_name,
        source_type="code",
        fallback_chunk_type="java_fallback",
        declaration_re=JAVA_DECL_RE,
        language="java",
        module_chunk_type="java_package",
        chunk_type_for_kind={
            "class": "java_class",
            "interface": "java_interface",
            "enum": "java_class",
            "record": "java_class",
            "function": "java_method",
        },
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
        doc_type = "api_doc" if source_type == "api_doc" else "config"
        language = _language_from_path(lower)
        keys = list(data.keys())
        key_ranges = _top_level_key_ranges(content, lower, keys)
        chunks: list[RawChunk] = []
        for key in keys[:MAX_CONFIG_KEY_CHUNKS]:
            text = _render_structured_section(key, data[key])
            if not text.strip():
                continue
            start_line, end_line = key_ranges.get(key, (1, len(lines)))
            chunks.append(
                RawChunk(
                    text=text,
                    chunk_type=_config_chunk_type(str(key), source_type),
                    source_type=source_type,
                    document_path=file_path,
                    doc_type=doc_type,
                    service_name=service_name,
                    section_title=f"config {key}",
                    start_line=start_line,
                    end_line=end_line,
                    metadata={"language": language, "config_key": key, "kind": "config"},
                )
            )
        overflow_keys = keys[MAX_CONFIG_KEY_CHUNKS:]
        if overflow_keys:
            # Keep keys beyond the per-key limit searchable in one bounded chunk.
            overflow_text = "\n\n".join(_render_structured_section(key, data[key]) for key in overflow_keys)
            located = [key_ranges[key] for key in overflow_keys if key in key_ranges]
            chunks.append(
                RawChunk(
                    text=overflow_text,
                    chunk_type="config_section",
                    source_type=source_type,
                    document_path=file_path,
                    doc_type=doc_type,
                    service_name=service_name,
                    section_title=f"config overflow ({len(overflow_keys)} keys)",
                    start_line=min(start for start, _ in located) if located else 1,
                    end_line=max(end for _, end in located) if located else len(lines),
                    metadata={
                        "language": language,
                        "config_keys": [str(key) for key in overflow_keys],
                        "kind": "config_overflow",
                    },
                )
            )
        if chunks:
            return chunks

    return [
        RawChunk(
            text=content,
            chunk_type="config_fallback",
            source_type=source_type,
            document_path=file_path,
            doc_type="api_doc" if source_type == "api_doc" else "config",
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
    module_chunk_type: str,
    chunk_type_for_kind: dict[str, str],
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
        resolved = _resolve_regex_declaration(groups, language)
        if resolved is None:
            continue
        declarations.append((index, *resolved))

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
                    chunk_type=module_chunk_type,
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
                chunk_type=chunk_type_for_kind.get(kind, fallback_chunk_type),
                source_type=source_type,
                document_path=file_path,
                doc_type="code",
                service_name=service_name,
                section_title=f"{kind} {symbol}",
                start_line=start_line,
                end_line=end_line,
                metadata={"language": language, "symbol": symbol, "symbol_name": symbol, "kind": kind},
            )
        )
    return chunks


def _resolve_regex_declaration(groups: list[str], language: str) -> tuple[str, str] | None:
    if language in {"js", "ts", "jsx", "tsx"}:
        if groups[0] in {"class", "interface", "type", "enum"}:
            return groups[0], groups[1]
        for symbol in groups[3:]:
            if symbol:
                return "function", symbol
        return "function", groups[-1]
    if len(groups) < 2:
        return None
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
    schemas = data.get("components", {}).get("schemas", {}) if isinstance(data.get("components"), dict) else {}
    if not isinstance(paths, dict):
        paths = {}
    if not isinstance(schemas, dict):
        schemas = {}
    if not paths and not schemas:
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
                chunk_type="openapi_endpoint",
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
    for schema_name, value in list(schemas.items())[:100]:
        chunks.append(
            RawChunk(
                text=_render_structured_section(str(schema_name), value),
                chunk_type="openapi_schema",
                source_type="api_doc",
                document_path=file_path,
                doc_type="api_doc",
                service_name=service_name,
                section_title=f"schema {schema_name}",
                start_line=1,
                end_line=len(lines),
                metadata={
                    "language": _language_from_path(file_path.lower()),
                    "symbol_name": str(schema_name),
                    "kind": "openapi_schema",
                },
            )
        )
    return chunks


def _top_level_key_ranges(content: str, lower_path: str, keys: list) -> dict:
    """Map each top-level key to its 1-based (start, end) line range.

    Keys are located in document order; a key ends just before the next
    located key (or at EOF). Keys that cannot be located are omitted and the
    caller falls back to the whole file.
    """
    lines = content.splitlines()
    starts: dict = {}
    if lower_path.endswith((".yaml", ".yml")):
        try:
            node = yaml.compose(content)
        except yaml.YAMLError:
            node = None
        if isinstance(node, yaml.MappingNode):
            values = {str(key): key for key in keys}
            for key_node, _ in node.value:
                key = values.get(str(key_node.value))
                if key is not None and key not in starts:
                    starts[key] = key_node.start_mark.line + 1
    else:
        cursor = 0
        indent: str | None = None
        for key in keys:
            pattern = _key_line_pattern(str(key), lower_path, indent)
            if pattern is None:
                break
            for index in range(cursor, len(lines)):
                match = pattern.match(lines[index])
                if match:
                    if indent is None and lower_path.endswith(".json"):
                        indent = match.group(1)
                    starts[key] = index + 1
                    cursor = index + 1
                    break

    ordered = sorted(starts.items(), key=lambda item: item[1])
    ranges: dict = {}
    for position, (key, start) in enumerate(ordered):
        end = ordered[position + 1][1] - 1 if position + 1 < len(ordered) else len(lines)
        while end > start and not lines[end - 1].strip():
            end -= 1
        ranges[key] = (start, end)
    return ranges


def _key_line_pattern(key: str, lower_path: str, json_indent: str | None) -> re.Pattern[str] | None:
    escaped = re.escape(key)
    if lower_path.endswith(".json"):
        indent = re.escape(json_indent) if json_indent is not None else r"\s*"
        return re.compile(rf'^({indent})(?:\{{\s*)?"{escaped}"\s*:')
    if lower_path.endswith(".toml"):
        name = rf'(?:{escaped}|"{escaped}"|\'{escaped}\')'
        return re.compile(rf"^\s*(?:\[\[?\s*{name}\s*[\].]|{name}\s*[.=])")
    if lower_path.endswith(".ini"):
        return re.compile(rf"^\s*\[\s*{escaped}\s*\]")
    return None


def _config_chunk_type(key: str, source_type: str) -> str:
    normalized = key.lower()
    if source_type == "api_doc":
        return "openapi_schema"
    if normalized in {"service", "services", "application", "server"}:
        return "config_service_block"
    if normalized in {"env", "environment", "environmentvariables", "environment_variables"}:
        return "env_var_block"
    if normalized in {"dependency", "dependencies", "require", "requires"}:
        return "dependency_block"
    return "config_section"


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

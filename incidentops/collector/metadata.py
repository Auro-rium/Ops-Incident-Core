"""Deterministic, bounded metadata extraction for Collector documents."""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from .taxonomy import language

_GO_PACKAGE = re.compile(r"^\s*package\s+(\w+)", re.MULTILINE)
_GO_FUNCTION = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", re.MULTILINE)
_GO_TYPE = re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)", re.MULTILINE)
_PROTO_SYMBOL = re.compile(r"^\s*(service|rpc|message|enum)\s+([A-Za-z_]\w*)", re.MULTILINE)
_PY_SYMBOL = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)", re.MULTILINE)
_TS_SYMBOL = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
_JAVA_PACKAGE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;", re.MULTILINE)
_JAVA_TYPE = re.compile(
    r"^\s*(?:(?:public|private|protected|abstract|final|static)\s+)*(?:class|interface|enum|record)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
_JAVA_METHOD = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract|native)\s+)+[A-Za-z_][\w<>\[\], ?]*\s+([A-Za-z_]\w*)\s*\(",
    re.MULTILINE,
)
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_HTTP_ENDPOINT = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+(/[^\s]+)")
_OPENAPI_PATH = re.compile(r"^\s{0,4}(/[^\s:]+):\s*(?:#.*)?$", re.MULTILINE)
_LOG_LEVEL = re.compile(r"\b(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\b")
_ERROR_CODE = re.compile(r"\b(?:ERR_[A-Z0-9_]+|E\d{3,5})\b")
_COMMIT = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_TRACE_ID = re.compile(r"\b(?:trace(?:_id)?|trace-id)[=:]([A-Za-z0-9._-]+)", re.IGNORECASE)
_REQUEST_ID = re.compile(r"\b(?:request(?:_id)?|request-id)[=:]([A-Za-z0-9._-]+)", re.IGNORECASE)
_CONFIG_KEY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*(?::|=)", re.MULTILINE)
_RELEASE_MARKER = re.compile(r"\b(?:release|version|changelog)\b[^\n]{0,120}", re.IGNORECASE)
_SERVICE_PATH = re.compile(r"(?:^|/)(?:services?|cmd)/([A-Za-z0-9_-]+)(?:/|$)", re.IGNORECASE)


def extract(
    path: str,
    content: str,
    repo_name: str | None = None,
    branch: str | None = None,
    commit_sha: str | None = None,
) -> dict:
    """Extract explainable facts only; no model inference or guessed metadata."""
    language_name = language(path)
    posix = PurePosixPath(path)
    symbols, function_names, class_names, package_name = _source_symbols(language_name, content)
    commit_matches = _COMMIT.findall(content)
    timestamps = _TIMESTAMP.findall(content)
    endpoint_candidates = sorted(set([*_HTTP_ENDPOINT.findall(content), *_OPENAPI_PATH.findall(content)]))[:50]
    package_path = str(posix.parent) if str(posix.parent) not in {"", "."} else None
    result = {
        "repo_name": repo_name,
        "branch": branch,
        "commit_sha": commit_sha or (commit_matches[0].lower() if commit_matches else None),
        "file_language": language_name,
        "language": language_name,
        "module_path": str(posix.with_suffix("")),
        "package_path": package_path,
        "package_name": package_name,
        "symbol_names": sorted(set(symbols))[:100],
        "function_names": sorted(set(function_names))[:100],
        "class_names": sorted(set(class_names))[:100],
        "headings": _HEADING.findall(content)[:50],
        "endpoint_candidates": endpoint_candidates,
        "api_paths": [candidate for candidate in endpoint_candidates if candidate.startswith("/")][:50],
        "service_name": _service_from_path(path),
        "log_levels": sorted(set(_LOG_LEVEL.findall(content))),
        "error_codes": sorted(set(_ERROR_CODE.findall(content)))[:50],
        "trace_ids": sorted(set(_TRACE_ID.findall(content)))[:100],
        "request_ids": sorted(set(_REQUEST_ID.findall(content)))[:100],
        "timestamp_start": timestamps[0] if timestamps else None,
        "timestamp_end": timestamps[-1] if timestamps else None,
        "config_keys_summary": sorted(set(_CONFIG_KEY.findall(content)))[:100],
        "release_markers": _release_markers(path, content),
    }
    return {key: value for key, value in result.items() if value not in (None, [], "")}


def _source_symbols(language_name: str | None, content: str) -> tuple[list[str], list[str], list[str], str | None]:
    if language_name == "go":
        package = _GO_PACKAGE.search(content)
        functions = _GO_FUNCTION.findall(content)
        classes = _GO_TYPE.findall(content)
        return [*classes, *functions], functions, classes, package.group(1) if package else None
    if language_name == "proto":
        matches = _PROTO_SYMBOL.findall(content)
        symbols = [name for _, name in matches]
        return symbols, [name for kind, name in matches if kind == "rpc"], [], None
    if language_name == "python":
        return _python_symbols(content)
    if language_name in {"typescript", "javascript"}:
        symbols = _TS_SYMBOL.findall(content)
        classes = re.findall(r"^\s*(?:export\s+)?(?:class|interface|type)\s+([A-Za-z_]\w*)", content, re.MULTILINE)
        return symbols, [name for name in symbols if name not in classes], classes, None
    if language_name == "java":
        package = _JAVA_PACKAGE.search(content)
        classes = _JAVA_TYPE.findall(content)
        functions = _JAVA_METHOD.findall(content)
        return [*classes, *functions], functions, classes, package.group(1) if package else None
    return [], [], [], None


def _python_symbols(content: str) -> tuple[list[str], list[str], list[str], str | None]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        names = _PY_SYMBOL.findall(content)
        return names, names, [], None
    functions: list[str] = []
    classes: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
    return [*classes, *functions], functions, classes, None


def _service_from_path(path: str) -> str | None:
    match = _SERVICE_PATH.search(path.replace("\\", "/"))
    return match.group(1) if match else None


def _release_markers(path: str, content: str) -> list[str]:
    markers = _RELEASE_MARKER.findall(content)
    filename = PurePosixPath(path).name.lower()
    if any(token in filename for token in ("changelog", "release", "version")):
        markers.append(f"release_file:{filename}")
    return sorted({marker.strip() for marker in markers if marker.strip()})[:25]

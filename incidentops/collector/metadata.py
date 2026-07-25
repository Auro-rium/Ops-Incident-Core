from __future__ import annotations

import re
from pathlib import PurePosixPath

from .taxonomy import language

_GO_PACKAGE = re.compile(r"^\s*package\s+(\w+)", re.MULTILINE)
_GO_FUNCTION = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", re.MULTILINE)
_GO_TYPE = re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)", re.MULTILINE)
_PROTO_SYMBOL = re.compile(r"^\s*(service|rpc|message|enum)\s+([A-Za-z_]\w*)", re.MULTILINE)
_PY_SYMBOL = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)", re.MULTILINE)
_TS_SYMBOL = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type)\s+([A-Za-z_]\w*)", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_ENDPOINT = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_./{}-]+)")
_LOG_LEVEL = re.compile(r"\b(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\b")
_ERROR_CODE = re.compile(r"\b(?:ERR_[A-Z0-9_]+|E\d{3,5})\b")
_COMMIT = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)


def extract(path: str, content: str, repo_name: str | None = None, branch: str | None = None, commit_sha: str | None = None) -> dict:
    suffix_language = language(path)
    posix = PurePosixPath(path)
    symbols: list[str] = []
    package_name = None
    if suffix_language == "go":
        package = _GO_PACKAGE.search(content)
        package_name = package.group(1) if package else None
        symbols = [*(_GO_TYPE.findall(content)), *(_GO_FUNCTION.findall(content))]
    elif suffix_language == "proto":
        symbols = [name for _, name in _PROTO_SYMBOL.findall(content)]
    elif suffix_language == "python":
        symbols = _PY_SYMBOL.findall(content)
    elif suffix_language in {"typescript", "javascript"}:
        symbols = _TS_SYMBOL.findall(content)
    commit_matches = _COMMIT.findall(content)
    result = {
        "repo_name": repo_name,
        "branch": branch,
        "commit_sha": commit_sha or (commit_matches[0].lower() if commit_matches else None),
        "file_language": suffix_language,
        "language": suffix_language,
        "module_path": str(posix.with_suffix("")),
        "package_name": package_name,
        "symbol_names": sorted(set(symbols))[:100],
        "headings": _HEADING.findall(content)[:50],
        "endpoint_candidates": sorted(set(_ENDPOINT.findall(content)))[:50],
        "log_levels": sorted(set(_LOG_LEVEL.findall(content))),
        "error_codes": sorted(set(_ERROR_CODE.findall(content)))[:50],
    }
    return {key: value for key, value in result.items() if value not in (None, [], "")}

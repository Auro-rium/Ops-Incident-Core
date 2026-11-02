from __future__ import annotations

from pathlib import PurePosixPath

from incidentops.ingestion.chunking.metadata import CODE_SUFFIXES, classify_source_type, path_segments

CANONICAL_SOURCE_TYPES = {
    "code", "api_doc", "config", "logs", "deploy", "runbook", "incident", "unknown_text"
}

_DEPLOY_DIRECTORIES = {"deploy", "release"}
_DEPLOY_PREFIXES = ("deploy-", "release-")

LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".java": "java", ".proto": "proto",
    ".rs": "rust", ".sql": "sql", ".yaml": "yaml", ".yml": "yaml",
    ".json": "json", ".toml": "toml", ".ini": "ini", ".md": "markdown",
    ".log": "log", ".diff": "diff", ".patch": "diff",
}


def classify(path: str, content: str = "") -> str:
    normalized = path.replace("\\", "/")
    lower = normalized.lower()
    if _is_deploy_path(lower):
        return "deploy"
    value = classify_source_type(normalized)
    if value == "unknown":
        value = "unknown_text"
    if value in CANONICAL_SOURCE_TYPES:
        return value
    if lower.endswith((".json", ".yaml", ".yml", ".toml", ".ini")):
        return "config"
    return "unknown_text"


def _is_deploy_path(lower: str) -> bool:
    """Deploy evidence by whole path segment; source code is never a deploy."""
    if lower.endswith((*CODE_SUFFIXES, ".proto")):
        return False
    segments = path_segments(lower)
    directories, name = segments[:-1], segments[-1] if segments else ""
    if any(segment in _DEPLOY_DIRECTORIES for segment in directories):
        return True
    return any(segment.startswith(_DEPLOY_PREFIXES) for segment in [*directories, name])


def language(path: str) -> str | None:
    return LANGUAGE_BY_SUFFIX.get(PurePosixPath(path.lower()).suffix)

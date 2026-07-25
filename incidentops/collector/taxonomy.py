from __future__ import annotations

from pathlib import PurePosixPath

from incidentops.ingestion.chunking.metadata import classify_source_type

CANONICAL_SOURCE_TYPES = {
    "code", "api_doc", "config", "logs", "deploy", "runbook", "incident", "unknown_text"
}

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
    if any(
        token in lower
        for token in ("deploy/", "release/", "/deploy/", "/release/", "deploy-", "release-")
    ):
        return "deploy"
    value = classify_source_type(normalized)
    if value == "unknown":
        value = "unknown_text"
    if value in CANONICAL_SOURCE_TYPES:
        return value
    if lower.endswith((".json", ".yaml", ".yml", ".toml", ".ini")):
        return "config"
    return "unknown_text"


def language(path: str) -> str | None:
    return LANGUAGE_BY_SUFFIX.get(PurePosixPath(path.lower()).suffix)

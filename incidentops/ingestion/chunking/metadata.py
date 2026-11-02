"""
Metadata enrichment helpers for arbitrary incident data.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

DEPLOY_HASH_RE = re.compile(r"\b([a-f0-9]{6,40})\b", re.IGNORECASE)
ENDPOINT_RE = re.compile(r"(?:GET|POST|PUT|DELETE|PATCH)\s+(/\S+)")
SERVICE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]{2,40}$")
COMMON_PATH_STOPWORDS = {
    "logs",
    "log",
    "docs",
    "doc",
    "services",
    "service",
    "src",
    "app",
    "code",
    "lib",
    "libs",
    "api",
    "runbooks",
    "incidents",
    "deploys",
    "deploy",
    "patches",
    "diffs",
    "tests",
    "fixtures",
    "basic_incident",
    "utils",
    "helper",
    "helpers",
    "common",
}


CODE_SUFFIXES = (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".rb")
INCIDENT_DIRECTORY_SEGMENTS = {"incidents", "postmortems"}
INCIDENT_TEXT_SUFFIXES = (".md", ".txt")
DEPLOY_JSON_TOKENS = {"deploy", "deploys", "deployment", "deployments", "release", "releases", "commit", "commits"}
CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
PATH_TOKEN_RE = re.compile(r"[a-z0-9]+")


def path_segments(path: str) -> list[str]:
    """Lower-cased path components, tolerant of Windows separators."""
    return [part for part in path.replace("\\", "/").lower().split("/") if part and part not in (".", "..")]


def is_incident_path(path: str) -> bool:
    """Markdown/text under an ``incidents/`` or ``postmortems/`` directory.

    Matching whole directory segments keeps paths such as ``incidentops/``
    or ``docs/postmortem-template.md`` out of the incident parser.
    """
    lower = path.lower()
    if not lower.endswith(INCIDENT_TEXT_SUFFIXES):
        return False
    return any(segment in INCIDENT_DIRECTORY_SEGMENTS for segment in path_segments(lower)[:-1])


def extract_service_from_path(path: str) -> str | None:
    """Infer a likely service name from a path without hardcoded service lists."""
    normalized = PurePosixPath(path.replace("\\", "/"))
    parts = [part.lower() for part in normalized.parts if part and part not in (".", "..")]
    candidates: list[str] = []
    for idx, part in enumerate(parts):
        stem = part.rsplit(".", 1)[0]
        if part in COMMON_PATH_STOPWORDS:
            if idx + 1 < len(parts):
                nxt = parts[idx + 1]
                if SERVICE_TOKEN_RE.match(nxt) and nxt not in COMMON_PATH_STOPWORDS:
                    candidates.append(nxt)
            continue
        if SERVICE_TOKEN_RE.match(part) and part not in COMMON_PATH_STOPWORDS:
            candidates.append(part)
        elif stem and stem != part:
            for token in re.split(r"[-_.]", stem):
                if SERVICE_TOKEN_RE.match(token) and token not in COMMON_PATH_STOPWORDS:
                    candidates.append(token)
    return candidates[0] if candidates else None


def extract_deploy_hash(text: str) -> str | None:
    explicit = re.search(
        r"(?:deploy|release|commit|sha|revision)[_\s:=#-]+([a-f0-9]{6,40})",
        text,
        re.IGNORECASE,
    )
    if explicit:
        return explicit.group(1).lower()
    generic = DEPLOY_HASH_RE.search(text)
    return generic.group(1).lower() if generic else None


def extract_endpoints(text: str) -> list[str]:
    return ENDPOINT_RE.findall(text)


def is_deploy_json_path(path: str) -> bool:
    """JSON named for deploys by whole path token, so ``committee.json`` stays config."""
    if not path.lower().endswith(".json"):
        return False
    words = CAMEL_BOUNDARY_RE.sub(" ", path).lower()
    return any(token in DEPLOY_JSON_TOKENS for token in PATH_TOKEN_RE.findall(words))


def classify_source_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(CODE_SUFFIXES):
        return "code"
    if lower.endswith(".proto"):
        return "api_doc"
    if lower.endswith(".log") or "/logs/" in lower:
        return "logs"
    if lower.endswith((".patch", ".diff")):
        return "deploy"
    if is_deploy_json_path(path):
        return "deploy"
    if is_incident_path(lower):
        return "incident"
    if "openapi" in lower or "swagger" in lower:
        return "api_doc"
    if lower.endswith((".yaml", ".yml")):
        if "openapi" in lower or "swagger" in lower:
            return "api_doc"
        return "config"
    if lower.endswith((".toml", ".ini")):
        return "config"
    if lower.endswith(".json") and any(token in lower for token in ("openapi", "swagger")):
        return "api_doc"
    if lower.endswith((".md", ".txt")):
        return "runbook"
    if lower.endswith(".json"):
        return "config"
    return "unknown"


def classify_doc_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(CODE_SUFFIXES):
        return "code"
    if lower.endswith(".proto"):
        return "api_doc"
    if lower.endswith(".log") or "/logs/" in lower:
        return "log"
    if lower.endswith((".patch", ".diff")) or is_deploy_json_path(path):
        return "deploy"
    if is_incident_path(lower):
        return "incident"
    if lower.endswith((".yaml", ".yml")):
        if "openapi" in lower or "swagger" in lower:
            return "api_doc"
        return "config"
    if lower.endswith((".toml", ".ini", ".json")):
        if "openapi" in lower or "swagger" in lower:
            return "api_doc"
        return "config"
    return "markdown"

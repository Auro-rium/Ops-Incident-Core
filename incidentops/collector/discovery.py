"""Discovery, skip and normalization rules shared by the Collector and local ingest."""

from __future__ import annotations

import fnmatch
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from incidentops.config.settings import Settings

from .metadata import extract as extract_metadata
from .models import DiscoveredFile
from .redaction import redact
from .taxonomy import classify

_DENIED_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", "target", ".next", ".pytest_cache"}
_DENIED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".db", ".zip", ".gz", ".png", ".jpg", ".jpeg", ".pdf"}


def discover_files(
    root: Path,
    *,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> Iterator[DiscoveredFile]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"collector root is not a directory: {root}")
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if include_paths and not any(fnmatch.fnmatchcase(relative, pattern) for pattern in include_paths):
            continue
        if exclude_paths and any(fnmatch.fnmatchcase(relative, pattern) for pattern in exclude_paths):
            continue
        stat = path.stat()
        yield DiscoveredFile(str(path), relative, stat.st_size, datetime.fromtimestamp(stat.st_mtime, timezone.utc))


def skip_reason(item: DiscoveredFile, settings: Settings, *, max_bytes: int | None = None) -> str | None:
    parts = set(Path(item.relative_path).parts)
    if parts & _DENIED_PARTS:
        return "generated_file" if parts & {"dist", "build", "target", ".next"} else "vendor_file"
    suffix = Path(item.relative_path).suffix.lower()
    if suffix in _DENIED_SUFFIXES or Path(item.relative_path).name.startswith(".env"):
        return "unsupported_extension"
    if item.size_bytes == 0:
        return "empty"
    if item.size_bytes > (settings.max_document_bytes if max_bytes is None else max_bytes):
        return "oversized"
    if suffix and suffix not in settings.supported_extensions_set:
        return "unsupported_extension"
    try:
        with open(item.absolute_path, "rb") as handle:
            sample = handle.read(4096)
    except OSError:
        return "read_failed"
    if b"\x00" in sample:
        return "binary"
    return None


def normalize_file(item: DiscoveredFile, **repo_metadata: str | None) -> tuple[dict | None, str | None]:
    """Read strictly as UTF-8, redact, and build the normalized document payload."""
    try:
        text = Path(item.absolute_path).read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None, "read_failed"
    if not text.strip():
        return None, "empty"
    try:
        content, redaction_count = redact(text)
    except Exception:
        return None, "redaction_failed"
    metadata = extract_metadata(item.relative_path, content, **repo_metadata)
    metadata["redaction_count"] = redaction_count
    encoded = content.encode("utf-8")
    return {
        "external_id": item.relative_path,
        "path": item.relative_path,
        "source_type": classify(item.relative_path, content),
        "content": content,
        "content_hash": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
        "modified_at": item.modified_at.isoformat() if item.modified_at else None,
        "metadata": metadata,
    }, None

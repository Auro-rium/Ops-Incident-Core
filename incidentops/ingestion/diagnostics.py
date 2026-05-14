from __future__ import annotations

from collections import Counter
from pathlib import Path

from incidentops.config.settings import Settings, get_settings
from incidentops.ingestion.chunking.metadata import classify_source_type


def build_source_coverage(source_type_counts: dict[str, int]) -> dict:
    has_logs = source_type_counts.get("logs", 0) > 0
    has_code = source_type_counts.get("code", 0) > 0
    has_deploys = source_type_counts.get("deploy", 0) > 0
    has_incidents = source_type_counts.get("incident", 0) > 0
    has_runbooks = (source_type_counts.get("runbook", 0) + source_type_counts.get("api_doc", 0)) > 0
    warnings: list[str] = []
    if not has_logs:
        warnings.append("No logs found. Runtime symptom analysis may be weak.")
    if not has_code:
        warnings.append("No code found. Code-path and diff reasoning will be limited.")
    if not has_deploys:
        warnings.append("No deploy history found. Deploy-regression investigations may be weak.")
    if not has_incidents:
        warnings.append("No previous incidents found. Similar-incident lookup unavailable.")
    if not has_runbooks:
        warnings.append("No runbooks or API docs found. Ownership and remediation guidance may be weak.")
    return {
        "has_logs": has_logs,
        "has_code": has_code,
        "has_deploys": has_deploys,
        "has_incidents": has_incidents,
        "has_runbooks": has_runbooks,
        "warnings": warnings,
    }


def inspect_folder(data_path: str, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    base = Path(data_path).expanduser().resolve()
    if not base.exists():
        raise FileNotFoundError(f"Folder not found: {base}")

    file_type_counts: Counter[str] = Counter()
    source_type_counts: Counter[str] = Counter()
    unsupported_files: list[dict] = []
    oversized_files: list[dict] = []
    total_files_seen = 0
    ingestable_files = 0

    for path in sorted(base.rglob("*")):
        if not path.is_file() or "__pycache__" in str(path) or path.name.startswith("."):
            continue
        total_files_seen += 1
        rel_path = str(path.relative_to(base))
        suffix = path.suffix.lower() or "<none>"
        file_type_counts[suffix] += 1
        size_bytes = path.stat().st_size
        if suffix not in settings.supported_extensions_set:
            unsupported_files.append({"path": rel_path, "reason": "unsupported_extension", "size_bytes": size_bytes})
            continue
        if size_bytes > settings.max_ingest_file_bytes:
            oversized_files.append({"path": rel_path, "reason": "file_too_large", "size_bytes": size_bytes})
            continue
        ingestable_files += 1
        source_type_counts[classify_source_type(rel_path)] += 1

    coverage = build_source_coverage(dict(source_type_counts))
    estimated_ingestability = "high"
    if ingestable_files == 0:
        estimated_ingestability = "none"
    elif not coverage["has_logs"] and not coverage["has_deploys"] and not coverage["has_code"]:
        estimated_ingestability = "low"
    elif len(unsupported_files) > ingestable_files or oversized_files:
        estimated_ingestability = "medium"

    warnings = list(coverage["warnings"])
    if unsupported_files:
        warnings.append(f"{len(unsupported_files)} unsupported files will be skipped.")
    if oversized_files:
        warnings.append(f"{len(oversized_files)} oversized files exceed the ingest limit.")
    if ingestable_files == 0:
        warnings.append("No supported ingestable files found.")

    return {
        "data_path": str(base),
        "total_files_seen": total_files_seen,
        "ingestable_files": ingestable_files,
        "file_type_counts": dict(file_type_counts),
        "likely_source_types": dict(source_type_counts),
        "oversized_files": oversized_files,
        "unsupported_files": unsupported_files,
        "estimated_ingestability": estimated_ingestability,
        "warnings": warnings,
        "source_coverage": coverage,
    }

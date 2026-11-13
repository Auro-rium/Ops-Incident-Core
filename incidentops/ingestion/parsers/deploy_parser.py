"""
Deploy parser — handles deploy-history.json and .patch/.diff files.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from incidentops.ingestion.chunking.metadata import extract_service_from_path
from incidentops.ingestion.schemas import RawChunk


def parse_deploy_history(content: str, file_path: str) -> list[RawChunk]:
    """Parse a deploy-history.json file — each deploy entry becomes a chunk."""
    try:
        deploys = json.loads(content)
    except json.JSONDecodeError:
        return []
    if isinstance(deploys, dict):
        deploys = [deploys]
    if not isinstance(deploys, list):
        return []

    chunks: list[RawChunk] = []
    for i, deploy in enumerate(deploys):
        if not isinstance(deploy, dict):
            continue
        if not any(deploy.get(key) for key in ("deploy_hash", "commit_sha", "commit", "sha", "deployed_at")):
            continue
        deploy_hash = (
            deploy.get("deploy_hash")
            or deploy.get("commit_sha")
            or deploy.get("commit")
            or deploy.get("sha")
            or "unknown"
        )
        service_name = deploy.get("service_name") or deploy.get("service")
        deployed_at_str = deploy.get("deployed_at", "")
        changed_files = deploy.get("changed_files", [])
        if isinstance(changed_files, str):
            changed_files = [changed_files]
        if not isinstance(changed_files, list):
            changed_files = []

        text_parts = [
            f"Deploy: {deploy_hash}",
            f"Service: {service_name}",
            f"Author: {deploy.get('author', 'unknown')}",
            f"Deployed at: {deployed_at_str}",
            f"Commit: {deploy.get('commit_sha', 'unknown')}",
            f"Summary: {deploy.get('summary', '')}",
            f"Changed files: {', '.join(str(path) for path in changed_files)}",
        ]
        text = "\n".join(text_parts)

        deployed_at = _parse_deployed_at(deployed_at_str)

        chunks.append(
            RawChunk(
                text=text,
                chunk_type="deploy_commit",
                source_type="deploy",
                document_path=file_path,
                doc_type="deploy",
                service_name=service_name,
                deploy_hash=deploy_hash,
                timestamp_start=deployed_at,
                section_title=f"deploy {deploy_hash}",
                start_line=1,
                end_line=1,
                metadata={**deploy, "kind": "deploy_commit"},
            )
        )
    return chunks


def _parse_deployed_at(value) -> datetime | None:
    """Accept ISO-8601 strings or numeric epoch seconds/milliseconds; result is UTC-aware."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if abs(value) >= 1e12 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.lstrip("-").replace(".", "", 1).isdigit():
        try:
            return _parse_deployed_at(float(raw))
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_patch_file(content: str, file_path: str) -> list[RawChunk]:
    """Parse a .patch / .diff file — chunk by file diff."""
    # Extract deploy hash from filename if present
    deploy_hash = _extract_deploy_hash_from_path(file_path)

    # Split by diff headers
    file_diffs = re.split(r"(?=^diff --git )", content, flags=re.MULTILINE)
    chunks: list[RawChunk] = []

    for i, diff_section in enumerate(file_diffs):
        diff_section = diff_section.strip()
        if not diff_section:
            continue

        # Extract changed file path
        file_match = re.search(r"^diff --git a/(.+?) b/(.+?)$", diff_section, re.MULTILINE)
        changed_file = file_match.group(2) if file_match else f"section_{i}"
        service_name = _infer_service_from_path(changed_file)

        lines = diff_section.split("\n")
        chunks.append(
            RawChunk(
                text=diff_section,
                chunk_type="deploy_diff",
                source_type="deploy",
                document_path=file_path,
                doc_type="deploy",
                service_name=service_name,
                deploy_hash=deploy_hash,
                section_title=f"diff: {changed_file}",
                start_line=1,
                end_line=len(lines),
                metadata={
                    "changed_file": changed_file,
                    "is_new_file": "+++ /dev/null" not in diff_section
                    and "new file mode" in diff_section,
                },
            )
        )

    return chunks


def _extract_deploy_hash_from_path(path: str) -> str | None:
    """Try to extract a deploy hash from the filename."""
    match = re.search(r"(?:diff|deploy)[_-]([a-f0-9]{6,})", path, re.IGNORECASE)
    return match.group(1) if match else None


def _infer_service_from_path(path: str) -> str | None:
    return extract_service_from_path(path)

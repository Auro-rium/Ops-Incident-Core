from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from incidentops.config.settings import Settings
from incidentops.ingestion.normalized import safe_json_size

from .client import CoreCollectorClient
from .hints import build as build_hints
from .metadata import extract as extract_metadata
from .models import CollectorSummary, DiscoveredFile
from .redaction import redact
from .taxonomy import classify, language

_DENIED_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", "target", ".next", ".pytest_cache"}
_DENIED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".db", ".zip", ".gz", ".png", ".jpg", ".jpeg", ".pdf"}


class CollectorService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def inspect(
        self,
        root: Path,
        max_files: int | None = None,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> CollectorSummary:
        summary = CollectorSummary()
        for item in self._discover(root, max_files, include_paths=include_paths, exclude_paths=exclude_paths):
            summary.files_seen += 1
            reason = self._skip_reason(item)
            if reason:
                summary.skip(reason)
                continue
            summary.count(summary.source_type_counts, classify(item.relative_path))
            detected_language = language(item.relative_path)
            if detected_language:
                summary.count(summary.language_counts, detected_language)
        return summary

    def sync(
        self,
        root: Path,
        *,
        project_id: str,
        source_name: str,
        collector_name: str,
        environment: str,
        batch_size: int = 50,
        max_files: int | None = None,
        repo_name: str | None = None,
        branch: str | None = None,
        commit_sha: str | None = None,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> CollectorSummary:
        token = os.getenv("INCIDENTOPS_TOKEN", "").strip()
        base_url = os.getenv("INCIDENTOPS_API_URL", "").strip()
        if not token or not base_url:
            raise RuntimeError("INCIDENTOPS_API_URL and INCIDENTOPS_TOKEN are required for Collector sync")
        summary = CollectorSummary()
        client = CoreCollectorClient(base_url, token)
        try:
            repository_context = self._repository_context(
                root,
                repo_name=repo_name,
                branch=branch,
                commit_sha=commit_sha,
            )
            capabilities = client.capabilities()
            limits = capabilities.get("limits", {})
            batch_size = min(batch_size, int(limits.get("max_documents_per_batch") or batch_size))
            collector_id = client.register_collector(project_id, collector_name, environment, "core-collector/1.0.0")
            source_id = client.register_source(project_id, source_name)
            sync_id = client.start_sync(source_id, collector_id, summary.diagnostics())
            summary.source_id = source_id
            summary.sync_id = sync_id
            batch: list[dict] = []
            max_batch_bytes = int(limits.get("max_batch_bytes") or self.settings.max_batch_bytes)
            for item in self._discover(root, max_files, include_paths=include_paths, exclude_paths=exclude_paths):
                summary.files_seen += 1
                reason = self._skip_reason(item)
                if reason:
                    summary.skip(reason)
                    continue
                document, normalize_reason = self._normalize(item, **repository_context)
                if document is None:
                    summary.skip(normalize_reason or "malformed_content")
                    continue
                summary.documents_normalized += 1
                summary.redaction_count += int(document["metadata"].get("redaction_count", 0))
                summary.count(summary.source_type_counts, document["source_type"])
                document_language = document["metadata"].get("language")
                if isinstance(document_language, str):
                    summary.count(summary.language_counts, document_language)
                for hint in document["metadata"].get("chunking_hints", []):
                    hint_type = hint.get("type") if isinstance(hint, dict) else None
                    if isinstance(hint_type, str):
                        summary.count(summary.hint_counts, hint_type)
                if safe_json_size({"documents": [*batch, document]}) > max_batch_bytes and batch:
                    self._upload_batch(client, source_id, sync_id, collector_id, batch, summary)
                    batch = []
                batch.append(document)
                if len(batch) >= batch_size:
                    self._upload_batch(client, source_id, sync_id, collector_id, batch, summary)
                    batch = []
            if batch:
                self._upload_batch(client, source_id, sync_id, collector_id, batch, summary)
            finish_status = "partial_success" if summary.parser_error_count or summary.embedding_failures else "success"
            client.finish_sync(source_id, sync_id, collector_id, finish_status, summary.diagnostics())
            return summary
        except Exception:
            # Core records a failed sync when the upload operation reaches it. Do not leak document content.
            summary.warnings.append("sync failed; inspect Core sync diagnostics and Collector logs")
            raise
        finally:
            client.close()

    def _upload_batch(self, client: CoreCollectorClient, source_id: str, sync_id: str, collector_id: str, batch: list[dict], summary: CollectorSummary) -> None:
        response = client.upload(source_id, sync_id, collector_id, batch)
        created = int(response.get("created", 0))
        updated = int(response.get("updated", 0))
        unchanged = int(response.get("skipped_unchanged", 0))
        summary.documents_created += created
        summary.documents_updated += updated
        summary.documents_unchanged += unchanged
        summary.documents_synced += created + updated + unchanged
        if unchanged:
            summary.skipped_reasons["unchanged"] = summary.skipped_reasons.get("unchanged", 0) + unchanged
        summary.chunks_created += int(response.get("chunks_created", 0))
        diagnostics = response.get("diagnostics")
        if isinstance(diagnostics, dict):
            summary.record_core_diagnostics(diagnostics)
        summary.bytes_uploaded += sum(int(item["size_bytes"]) for item in batch)

    def _normalize(self, item: DiscoveredFile, **repo_metadata: str | None) -> tuple[dict | None, str | None]:
        try:
            text = Path(item.absolute_path).read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            return None, "read_failed"
        try:
            content, redaction_count = redact(text)
        except Exception:
            return None, "redaction_failed"
        source_type = classify(item.relative_path, content)
        metadata = extract_metadata(item.relative_path, content, **repo_metadata)
        hints = build_hints(item.relative_path, content, source_type)
        metadata["chunking_hints"] = hints[: self.settings.max_chunks_per_document]
        metadata["hints_are_advisory"] = True
        metadata["redaction_count"] = redaction_count
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return {
            "external_id": item.relative_path,
            "path": item.relative_path,
            "source_type": source_type,
            "content": content,
            "content_hash": content_hash,
            "size_bytes": len(content.encode("utf-8")),
            "modified_at": item.modified_at.isoformat() if item.modified_at else None,
            "metadata": metadata,
        }, None

    @staticmethod
    def _repository_context(
        root: Path,
        *,
        repo_name: str | None,
        branch: str | None,
        commit_sha: str | None,
    ) -> dict[str, str | None]:
        """Resolve Git facts when available; explicit caller values win."""
        root = root.resolve()
        git_root = _git_value(root, ["rev-parse", "--show-toplevel"])
        repository_root = Path(git_root) if git_root else root
        return {
            "repo_name": repo_name or repository_root.name,
            "branch": branch or _git_value(repository_root, ["branch", "--show-current"]),
            "commit_sha": commit_sha or _git_value(repository_root, ["rev-parse", "HEAD"]),
        }

    def _discover(
        self,
        root: Path,
        max_files: int | None,
        *,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> Iterator[DiscoveredFile]:
        root = root.resolve()
        if not root.is_dir():
            raise ValueError(f"collector root is not a directory: {root}")
        emitted = 0
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
            emitted += 1
            if max_files is not None and emitted >= max_files:
                return

    def _skip_reason(self, item: DiscoveredFile) -> str | None:
        parts = set(Path(item.relative_path).parts)
        if parts & _DENIED_PARTS:
            return "generated_file" if parts & {"dist", "build", "target", ".next"} else "vendor_file"
        suffix = Path(item.relative_path).suffix.lower()
        if suffix in _DENIED_SUFFIXES or Path(item.relative_path).name.startswith(".env"):
            return "unsupported_extension"
        if item.size_bytes == 0:
            return "empty"
        if item.size_bytes > self.settings.max_document_bytes:
            return "oversized"
        if suffix and suffix not in self.settings.supported_extensions_set:
            return "unsupported_extension"
        try:
            sample = Path(item.absolute_path).read_bytes()[:4096]
        except OSError:
            return "unknown"
        if b"\x00" in sample:
            return "binary"
        return None


def _git_value(root: Path, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value or None

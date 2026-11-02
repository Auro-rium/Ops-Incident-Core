from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterator, TypeVar

import httpx

from incidentops.config.settings import Settings
from incidentops.ingestion.normalized import safe_json_size

from .client import CoreCollectorClient
from .discovery import discover_files, normalize_file, skip_reason
from .models import CollectorSummary, DiscoveredFile
from .taxonomy import classify, language

logger = logging.getLogger("incidentops.collector")

T = TypeVar("T")

_MAX_RETRIES = 3
_RETRY_BASE_SECONDS = 1.0
_TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


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
        accepted = 0
        for item in self._discover(root, include_paths=include_paths, exclude_paths=exclude_paths):
            if max_files is not None and accepted >= max_files:
                summary.complete = False
                break
            summary.files_seen += 1
            reason = self._skip_reason(item)
            if reason:
                summary.skip(reason)
                continue
            accepted += 1
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
        summary.complete = not (include_paths or exclude_paths)
        client = CoreCollectorClient(base_url, token)
        source_id = collector_id = sync_id = None
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
            for item in self._discover(root, include_paths=include_paths, exclude_paths=exclude_paths):
                if max_files is not None and summary.documents_normalized >= max_files:
                    summary.complete = False
                    break
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
                if safe_json_size({"documents": [*batch, document]}) > max_batch_bytes and batch:
                    self._upload_batch(client, source_id, sync_id, collector_id, batch, summary)
                    batch = []
                batch.append(document)
                if len(batch) >= batch_size:
                    self._upload_batch(client, source_id, sync_id, collector_id, batch, summary)
                    batch = []
            if batch:
                self._upload_batch(client, source_id, sync_id, collector_id, batch, summary)
            degraded = summary.parser_error_count or summary.embedding_failures or summary.lost_evidence
            finish_status = "partial_success" if degraded else "success"
            self._with_retries(
                summary,
                lambda: client.finish_sync(source_id, sync_id, collector_id, finish_status, summary.diagnostics()),
            )
            return summary
        except Exception as exc:
            summary.warnings.append("sync failed; inspect Core sync diagnostics and Collector logs")
            summary.complete = False
            if sync_id is not None:
                self._finish_failed(client, source_id, sync_id, collector_id, summary, exc)
            raise
        finally:
            client.close()

    def _finish_failed(
        self,
        client: CoreCollectorClient,
        source_id: str,
        sync_id: str,
        collector_id: str,
        summary: CollectorSummary,
        exc: Exception,
    ) -> None:
        """Close the sync on Core. Only the class name is sent: messages can quote document content."""
        error = type(exc).__name__
        diagnostics = {**summary.diagnostics(), "error": error, "error_message": f"collector sync failed: {error}"}
        try:
            self._with_retries(
                summary,
                lambda: client.finish_sync(source_id, sync_id, collector_id, "failed", diagnostics),
            )
        except Exception as finish_exc:
            # The sync error is what the caller must see, not this one.
            logger.warning("could not mark sync %s failed: %s", sync_id, type(finish_exc).__name__)

    def _upload_batch(self, client: CoreCollectorClient, source_id: str, sync_id: str, collector_id: str, batch: list[dict], summary: CollectorSummary) -> None:
        response = self._with_retries(summary, lambda: client.upload(source_id, sync_id, collector_id, batch))
        created = int(response.get("created", 0))
        updated = int(response.get("updated", 0))
        unchanged = int(response.get("skipped_unchanged", 0))
        summary.documents_created += created
        summary.documents_updated += updated
        summary.documents_unchanged += unchanged
        summary.documents_synced += created + updated + unchanged
        if unchanged:
            summary.skip("unchanged", unchanged)
        summary.chunks_created += int(response.get("chunks_created", 0))
        diagnostics = response.get("diagnostics")
        if isinstance(diagnostics, dict):
            summary.record_core_diagnostics(diagnostics)
        rejected = {error.get("external_id") for error in response.get("errors") or [] if isinstance(error, dict)}
        summary.bytes_uploaded += sum(int(item["size_bytes"]) for item in batch if item["external_id"] not in rejected)

    @staticmethod
    def _with_retries(summary: CollectorSummary, call: Callable[[], T]) -> T:
        """Retry transient Core failures.

        Core skips content hashes it has already indexed, so a re-sent batch does
        not re-index them. With async indexing, a batch whose first attempt landed
        but is still queued can be queued and counted again.
        """
        retries = 0
        while True:
            try:
                result = call()
            except Exception as exc:
                if retries >= _MAX_RETRIES or not _is_transient(exc):
                    raise
                summary.retry_attempted += 1
                time.sleep(_RETRY_BASE_SECONDS * 2**retries)
                retries += 1
                continue
            if retries:
                summary.retry_succeeded += 1
            return result

    def _normalize(self, item: DiscoveredFile, **repo_metadata: str | None) -> tuple[dict | None, str | None]:
        return normalize_file(item, **repo_metadata)

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
        *,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> Iterator[DiscoveredFile]:
        return discover_files(root, include_paths=include_paths, exclude_paths=exclude_paths)

    def _skip_reason(self, item: DiscoveredFile) -> str | None:
        return skip_reason(item, self.settings)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS_CODES
    return isinstance(exc, httpx.TransportError)


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

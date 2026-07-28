"""Narrow Core API client used only by the explicit benchmark command.

This is intentionally separate from ``CoreCollectorClient``: production
Collector syncs never retrieve, diagnose, or query the Core RAG API.
"""

from __future__ import annotations

from typing import Any

import httpx


class CoreBenchmarkClient:
    def __init__(self, base_url: str, token: str, timeout_seconds: int = 60):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {token}"},
        )

    def close(self) -> None:
        self._client.close()

    def latest_sync(self, source_id: str) -> dict[str, Any]:
        response = self._client.get(f"/v1/sources/{source_id}/syncs/latest")
        response.raise_for_status()
        return response.json()

    def source_integrity(self, project_id: str, source_id: str) -> dict[str, Any]:
        response = self._client.get(f"/v1/projects/{project_id}/sources/{source_id}/integrity")
        response.raise_for_status()
        return response.json()

    def search(self, project_id: str, query: str) -> dict[str, Any]:
        response = self._client.post(
            "/v1/search",
            json={"project_id": project_id, "query": query, "top_k": 5, "debug": True},
        )
        response.raise_for_status()
        return response.json()

from __future__ import annotations

from typing import Any

import httpx


class CoreCollectorClient:
    """Small capability-aware HTTP client; it has no database or RAG access."""

    def __init__(self, base_url: str, token: str, timeout_seconds: int = 30):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {token}"},
        )

    def close(self) -> None:
        self._client.close()

    def capabilities(self) -> dict[str, Any]:
        response = self._client.get("/v1/capabilities")
        response.raise_for_status()
        return response.json()

    def register_collector(self, project_id: str, name: str, environment: str, version: str) -> str:
        response = self._client.post(
            f"/v1/projects/{project_id}/collectors/register",
            json={"name": name, "environment": environment, "version": version},
        )
        response.raise_for_status()
        return response.json()["collector_id"]

    def register_source(self, project_id: str, name: str, source_type: str = "filesystem") -> str:
        response = self._client.post(
            f"/v1/projects/{project_id}/sources",
            json={"name": name, "source_type": source_type, "sync_mode": "collector", "config": {}},
        )
        response.raise_for_status()
        payload = response.json()
        source_id = payload.get("source_id") or payload.get("id")
        if not isinstance(source_id, str) or not source_id:
            raise RuntimeError("Core source registration response did not include an identifier")
        return source_id

    def start_sync(self, source_id: str, collector_id: str, diagnostics: dict[str, Any]) -> str:
        response = self._client.post(
            f"/v1/sources/{source_id}/syncs/start",
            json={"collector_id": collector_id, "diagnostics": diagnostics},
        )
        response.raise_for_status()
        return response.json()["sync_id"]

    def upload(self, source_id: str, sync_id: str, collector_id: str, documents: list[dict]) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/sources/{source_id}/documents/batch",
            json={
                "sync_id": sync_id,
                "collector_id": collector_id,
                "collector_version": "core-collector/1.0.0",
                "schema_version": "1",
                "core_api_version": "0.5.0",
                "documents": documents,
            },
        )
        response.raise_for_status()
        return response.json()

    def finish_sync(self, source_id: str, sync_id: str, collector_id: str, status: str, diagnostics: dict[str, Any]) -> None:
        response = self._client.post(
            f"/v1/sources/{source_id}/syncs/{sync_id}/finish",
            json={"collector_id": collector_id, "status": status, "diagnostics": diagnostics},
        )
        response.raise_for_status()

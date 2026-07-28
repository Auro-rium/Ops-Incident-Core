"""Qdrant-backed vector storage for IncidentOps retrieval.

PostgreSQL remains authoritative for chunk text, metadata, permissions, and
citations. Qdrant stores vectors plus the minimal payload needed to filter and
map a vector hit back to an authorized PostgreSQL chunk.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from incidentops.config.settings import Settings, get_settings


class VectorStoreError(RuntimeError):
    """Safe, non-content-bearing vector-store failure."""


@dataclass(frozen=True)
class VectorHit:
    chunk_id: uuid.UUID
    score: float


def _headers(settings: Settings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.qdrant_api_key.strip():
        headers["api-key"] = settings.qdrant_api_key
    return headers


def _base_url(settings: Settings) -> str:
    configured = settings.qdrant_url.strip().rstrip("/")
    if not configured:
        raise VectorStoreError("Qdrant URL is not configured")
    return configured


class QdrantVectorStore:
    """Small Qdrant REST adapter with explicit payload and filter boundaries."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def collection(self) -> str:
        return self.settings.qdrant_collection

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self.settings.qdrant_timeout_seconds) as client:
                response = await client.get(f"{_base_url(self.settings)}/healthz", headers=_headers(self.settings))
            return response.status_code == 200
        except (httpx.HTTPError, VectorStoreError):
            return False

    async def ensure_collection(self, embedding_dim: int) -> None:
        base_url = _base_url(self.settings)
        headers = _headers(self.settings)
        try:
            async with httpx.AsyncClient(timeout=self.settings.qdrant_timeout_seconds) as client:
                response = await client.get(f"{base_url}/collections/{self.collection}", headers=headers)
                if response.status_code == 404:
                    create = await client.put(
                        f"{base_url}/collections/{self.collection}",
                        headers=headers,
                        json={"vectors": {"size": embedding_dim, "distance": "Cosine"}},
                    )
                    self._raise_for_status(create, "collection creation")
                else:
                    self._raise_for_status(response, "collection lookup")
                    configured_dim = _collection_dimension(response.json())
                    if configured_dim is not None and configured_dim != embedding_dim:
                        raise VectorStoreError(
                            "Qdrant collection dimension does not match the configured embedding dimension"
                        )
                for field_name in _FILTER_FIELDS:
                    payload_index = await client.put(
                        f"{base_url}/collections/{self.collection}/index",
                        headers=headers,
                        json={"field_name": field_name, "field_schema": "keyword"},
                    )
                    self._raise_for_status(payload_index, f"payload index creation for {field_name}")
        except httpx.HTTPError as exc:
            raise VectorStoreError("Qdrant collection setup failed") from exc

    async def upsert(self, points: list[dict[str, Any]], embedding_dim: int) -> None:
        if not points:
            return
        await self.ensure_collection(embedding_dim)
        try:
            async with httpx.AsyncClient(timeout=self.settings.qdrant_timeout_seconds) as client:
                response = await client.put(
                    f"{_base_url(self.settings)}/collections/{self.collection}/points?wait=true",
                    headers=_headers(self.settings),
                    json={"points": points},
                )
            self._raise_for_status(response, "point upsert")
        except httpx.HTTPError as exc:
            raise VectorStoreError("Qdrant point upsert failed") from exc

    async def search(
        self,
        project_id: uuid.UUID,
        query_embedding: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        must = [
            {"key": "project_id", "match": {"value": str(project_id)}},
            {"key": "index_version", "match": {"value": self.settings.vector_index_version}},
        ]
        for field_name, value in (filters or {}).items():
            if field_name in _FILTER_FIELDS and value is not None:
                must.append({"key": field_name, "match": {"value": str(value)}})
        try:
            async with httpx.AsyncClient(timeout=self.settings.qdrant_timeout_seconds) as client:
                response = await client.post(
                    f"{_base_url(self.settings)}/collections/{self.collection}/points/query",
                    headers=_headers(self.settings),
                    json={
                        "query": query_embedding,
                        "limit": top_k,
                        "with_payload": False,
                        "with_vector": False,
                        "filter": {"must": must},
                    },
                )
        except httpx.HTTPError as exc:
            raise VectorStoreError("Qdrant vector search failed") from exc
        if response.status_code == 404:
            return []
        self._raise_for_status(response, "vector search")
        points = response.json().get("result", {}).get("points", [])
        hits: list[VectorHit] = []
        for point in points:
            try:
                hits.append(VectorHit(chunk_id=uuid.UUID(str(point["id"])), score=float(point["score"])))
            except (KeyError, TypeError, ValueError):
                continue
        return hits

    async def delete_by_filter(self, project_id: uuid.UUID, **filters: str) -> None:
        must = [{"key": "project_id", "match": {"value": str(project_id)}}]
        for field_name, value in filters.items():
            if field_name not in _FILTER_FIELDS:
                raise VectorStoreError(f"unsupported Qdrant filter field: {field_name}")
            must.append({"key": field_name, "match": {"value": str(value)}})
        try:
            async with httpx.AsyncClient(timeout=self.settings.qdrant_timeout_seconds) as client:
                response = await client.post(
                    f"{_base_url(self.settings)}/collections/{self.collection}/points/delete?wait=true",
                    headers=_headers(self.settings),
                    json={"filter": {"must": must}},
                )
        except httpx.HTTPError as exc:
            raise VectorStoreError("Qdrant point deletion failed") from exc
        if response.status_code != 404:
            self._raise_for_status(response, "point deletion")

    @staticmethod
    def _raise_for_status(response: httpx.Response, operation: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise VectorStoreError(f"Qdrant {operation} failed: HTTP {response.status_code}") from exc


_FILTER_FIELDS = {
    "project_id",
    "source_id",
    "document_id",
    "source_type",
    "chunk_type",
    "path",
    "language",
    "symbol_name",
    "package_name",
    "service_name",
    "endpoint",
    "commit_sha",
    "deploy_hash",
    "content_hash",
    "index_version",
}


def chunk_point(
    chunk,
    embedding: list[float],
    *,
    index_version: str,
    document_path: str | None = None,
) -> dict[str, Any]:
    """Build a bounded Qdrant point without storing raw chunk text."""
    metadata = dict(chunk.metadata_json or {})
    path = document_path or metadata.get("document_path") or ""
    payload = {
        "project_id": str(chunk.project_id),
        "source_id": str(chunk.source_id) if chunk.source_id else "",
        "document_id": str(chunk.document_id),
        "source_type": str(metadata.get("source_type") or "unknown_text"),
        "chunk_type": str(chunk.chunk_type or "unknown"),
        "path": str(path),
        "language": str(metadata.get("language") or ""),
        "symbol_name": str(metadata.get("symbol_name") or metadata.get("function_name") or ""),
        "package_name": str(metadata.get("package_name") or metadata.get("module_path") or ""),
        "service_name": str(chunk.service_name or ""),
        "endpoint": str(chunk.endpoint or ""),
        "commit_sha": str(metadata.get("commit_sha") or ""),
        "deploy_hash": str(chunk.deploy_hash or ""),
        "content_hash": str(metadata.get("content_hash") or ""),
        "index_version": str(index_version),
    }
    return {"id": str(chunk.id), "vector": [float(value) for value in embedding], "payload": payload}


def _collection_dimension(payload: dict[str, Any]) -> int | None:
    vectors = payload.get("result", {}).get("config", {}).get("params", {}).get("vectors")
    if isinstance(vectors, dict) and isinstance(vectors.get("size"), int):
        return vectors["size"]
    return None

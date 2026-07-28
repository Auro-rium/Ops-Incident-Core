"""Remote model gateway for cloud-only production inference.

Core never downloads or loads production model artifacts. Azure ML endpoints
serve embeddings and reranking. Contract tests may inject a fake transport.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from incidentops.config.settings import get_settings
from incidentops.observability.metrics import incr, observe_latency
from incidentops.security.secret_redaction import redact_secrets


class RemoteModelError(RuntimeError):
    """A safe, non-content-bearing remote model failure."""


_managed_identity_access_token: str | None = None
_managed_identity_expires_at = 0.0


async def _headers(settings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.rag_remote_auth_mode == "managed_identity":
        headers["Authorization"] = f"Bearer {await _get_managed_identity_token()}"
    elif settings.rag_remote_auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {settings.rag_remote_api_key}"
    else:
        headers["api-key"] = settings.rag_remote_api_key
    return headers


async def _get_managed_identity_token() -> str:
    global _managed_identity_access_token, _managed_identity_expires_at
    if _managed_identity_access_token and time.time() < _managed_identity_expires_at - 60:
        return _managed_identity_access_token
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise RemoteModelError("managed identity support is not installed") from exc
    try:
        credential = DefaultAzureCredential()
        token = await __import__("asyncio").to_thread(credential.get_token, "https://ml.azure.com/.default")
        _managed_identity_access_token = token.token
        _managed_identity_expires_at = float(token.expires_on)
        return _managed_identity_access_token
    except Exception as exc:
        raise RemoteModelError(f"managed identity token acquisition failed: {exc.__class__.__name__}") from exc


async def remote_embed(texts: list[str]) -> list[list[float]]:
    settings = get_settings()
    if not settings.rag_embedding_endpoint or (
        settings.rag_remote_auth_mode != "managed_identity" and not settings.rag_remote_api_key
    ):
        raise RemoteModelError("remote embedding endpoint is not configured")
    started = time.perf_counter()
    payload = {
        "model_id": settings.embedding_model,
        "index_version": settings.vector_index_version,
        "inputs": [{"id": str(index), "text": text} for index, text in enumerate(texts)],
    }
    try:
        async with httpx.AsyncClient(timeout=float(settings.rag_remote_timeout_seconds)) as client:
            response = await client.post(
                settings.rag_embedding_endpoint,
                headers=await _headers(settings),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        vectors = data.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RemoteModelError("remote embedding response count mismatch")
        if any(not isinstance(vector, list) for vector in vectors):
            raise RemoteModelError("remote embedding response contains invalid vectors")
        if any(len(vector) != settings.embedding_dim for vector in vectors):
            raise RemoteModelError("remote embedding response dimension mismatch")
        incr("rag_remote_embedding_calls_total")
        observe_latency("rag_remote_embedding", (time.perf_counter() - started) * 1000)
        return [[float(value) for value in vector] for vector in vectors]
    except httpx.HTTPError as exc:
        incr("rag_remote_embedding_failures_total")
        raise RemoteModelError(f"remote embedding request failed: {exc.__class__.__name__}") from exc


async def remote_rerank(query: str, candidates: list[dict[str, Any]]) -> list[float]:
    settings = get_settings()
    if not settings.rag_reranker_endpoint or (
        settings.rag_remote_auth_mode != "managed_identity" and not settings.rag_remote_api_key
    ):
        raise RemoteModelError("remote reranker endpoint is not configured")
    started = time.perf_counter()
    payload = {
        "model_id": settings.reranker_model,
        "query": redact_secrets(query)[: settings.rag_rerank_max_query_chars],
        "candidates": [
            {
                "id": str(candidate["id"]),
                "text": redact_secrets(str(candidate["text"]))[: settings.rag_rerank_max_chars_per_candidate],
            }
            for candidate in candidates[: settings.rag_rerank_max_candidates]
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=float(settings.rag_remote_timeout_seconds)) as client:
            response = await client.post(
                settings.rag_reranker_endpoint,
                headers=await _headers(settings),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        scores = data.get("scores")
        if not isinstance(scores, list) or len(scores) != len(payload["candidates"]):
            raise RemoteModelError("remote reranker response count mismatch")
        incr("rag_remote_rerank_calls_total")
        observe_latency("rag_remote_rerank", (time.perf_counter() - started) * 1000)
        return [float(score) for score in scores]
    except httpx.HTTPError as exc:
        incr("rag_remote_rerank_failures_total")
        raise RemoteModelError(f"remote reranker request failed: {exc.__class__.__name__}") from exc

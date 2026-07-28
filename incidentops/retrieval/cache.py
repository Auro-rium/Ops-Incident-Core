"""Best-effort Redis cache for remote RAG model outputs.

Only derived, bounded vectors are cached. Raw document text, credentials, and
answer payloads are deliberately excluded. A Redis outage must never block
retrieval or indexing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from incidentops.config.settings import get_settings
from incidentops.observability.metrics import incr

logger = logging.getLogger("incidentops.retrieval.cache")
_client = None


def _key(namespace: str, value: str) -> str:
    settings = get_settings()
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"incidentops:rag:{namespace}:{settings.vector_index_version}:{settings.embedding_model}:{settings.rag_model_revision}:{digest}"


async def get_embedding(text: str) -> list[float] | None:
    client = await _redis_client()
    if client is None:
        return None
    try:
        value = await client.get(_key("embedding", text))
        if not value:
            incr("rag_embedding_cache_misses_total")
            return None
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            return None
        incr("rag_embedding_cache_hits_total")
        return [float(item) for item in parsed]
    except Exception:
        logger.debug("embedding cache get failed", exc_info=True)
        return None


async def set_embedding(text: str, vector: list[float]) -> None:
    client = await _redis_client()
    if client is None:
        return
    try:
        settings = get_settings()
        await client.set(_key("embedding", text), json.dumps(vector), ex=settings.rag_cache_ttl_seconds)
    except Exception:
        logger.debug("embedding cache set failed", exc_info=True)


async def get_project_json(namespace: str, project_id: UUID, value: str) -> dict[str, Any] | None:
    client = await _redis_client()
    if client is None:
        return None
    try:
        cached = await client.get(await _project_key(client, namespace, project_id, value))
        if not cached:
            incr(f"rag_{namespace}_cache_misses_total")
            return None
        parsed = json.loads(cached)
        if not isinstance(parsed, dict):
            return None
        incr(f"rag_{namespace}_cache_hits_total")
        return parsed
    except Exception:
        logger.debug("project cache get failed namespace=%s", namespace, exc_info=True)
        return None


async def set_project_json(namespace: str, project_id: UUID, value: str, payload: dict[str, Any]) -> None:
    client = await _redis_client()
    if client is None:
        return
    try:
        settings = get_settings()
        await client.set(
            await _project_key(client, namespace, project_id, value),
            json.dumps(payload, separators=(",", ":"), default=str),
            ex=settings.rag_cache_ttl_seconds,
        )
    except Exception:
        logger.debug("project cache set failed namespace=%s", namespace, exc_info=True)


async def invalidate_project(project_id: UUID) -> None:
    """Bump the project cache generation instead of scanning/deleting keys."""
    client = await _redis_client()
    if client is None:
        return
    try:
        await client.incr(_project_generation_key(project_id))
        incr("rag_project_cache_invalidations_total")
    except Exception:
        logger.debug("project cache invalidation failed", exc_info=True)


async def _project_key(client: Any, namespace: str, project_id: UUID, value: str) -> str:
    generation = await client.get(_project_generation_key(project_id)) or "0"
    settings = get_settings()
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"incidentops:rag:{namespace}:{project_id}:{generation}:{settings.vector_index_version}:{settings.embedding_model}:{settings.rag_model_revision}:{digest}"


def _project_generation_key(project_id: UUID) -> str:
    return f"incidentops:rag:project-generation:{project_id}"


async def _redis_client() -> Any | None:
    global _client
    settings = get_settings()
    if not settings.rag_cache_enabled or not settings.resolved_redis_url:
        return None
    if _client is None:
        try:
            import redis.asyncio as redis

            _client = redis.from_url(settings.resolved_redis_url, decode_responses=True)
        except Exception:
            logger.debug("RAG cache unavailable", exc_info=True)
            return None
    return _client

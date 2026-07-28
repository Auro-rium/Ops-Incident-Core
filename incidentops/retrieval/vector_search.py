"""Qdrant vector retrieval mapped back to authoritative PostgreSQL chunks."""

from __future__ import annotations

import uuid
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from incidentops.config.settings import get_settings
from incidentops.db.models import Chunk
from incidentops.observability.metrics import observe_latency
from incidentops.retrieval.vector_store import QdrantVectorStore


async def vector_search(
    db: AsyncSession,
    project_id: uuid.UUID,
    query_embedding: list[float],
    top_k: int = 20,
    filters: dict[str, Any] | None = None,
) -> list[dict]:
    """Return vector candidates with PostgreSQL-backed chunk metadata."""
    start = time.time()
    settings = get_settings()
    if settings.retrieval_backend != "qdrant":
        raise RuntimeError("Configured vector backend is unsupported")
    hits = await QdrantVectorStore(settings).search(project_id, query_embedding, top_k, filters)
    if not hits:
        observe_latency("vector_search", (time.time() - start) * 1000)
        return []
    chunk_ids = [hit.chunk_id for hit in hits]
    result = await db.execute(
        select(Chunk)
        .options(selectinload(Chunk.document))
        .where(Chunk.project_id == project_id, Chunk.id.in_(chunk_ids))
    )
    chunks = {chunk.id: chunk for chunk in result.scalars().all()}
    rows = [
        {"chunk": chunks[hit.chunk_id], "score": hit.score}
        for hit in hits
        if hit.chunk_id in chunks
    ]
    observe_latency("vector_search", (time.time() - start) * 1000)
    return rows

"""
Vector search — pgvector cosine similarity over chunk embeddings.
"""

from __future__ import annotations

import uuid
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from incidentops.config.settings import get_settings
from incidentops.db.models import Chunk, ChunkEmbedding
from incidentops.observability.metrics import observe_latency


async def vector_search(
    db: AsyncSession,
    project_id: uuid.UUID,
    query_embedding: list[float],
    top_k: int = 20,
    filters: dict[str, Any] | None = None,
) -> list[dict]:
    """
    Cosine-similarity search using pgvector.
    Returns list of {chunk, score} dicts.
    """
    start = time.time()
    settings = get_settings()
    if settings.rag_retrieval_version == "v2":
        distance = ChunkEmbedding.embedding.cosine_distance(query_embedding)
        stmt = (
            select(Chunk, (1 - distance).label("score"))
            .join(ChunkEmbedding, ChunkEmbedding.chunk_id == Chunk.id)
            .options(selectinload(Chunk.document))
            .where(Chunk.project_id == project_id)
            .where(ChunkEmbedding.index_version == settings.rag_index_version)
            .where(ChunkEmbedding.model_id == settings.embedding_model)
            .where(ChunkEmbedding.status == "published")
        )
    else:
        distance = Chunk.embedding.cosine_distance(query_embedding)
        stmt = (
            select(Chunk, (1 - distance).label("score"))
            .options(selectinload(Chunk.document))
            .where(Chunk.project_id == project_id)
            .where(Chunk.embedding.isnot(None))
        )

    if filters:
        stmt = _apply_filters(stmt, filters)

    stmt = stmt.order_by(distance).limit(top_k)
    result = await db.execute(stmt)
    rows = [{"chunk": row[0], "score": float(row[1])} for row in result.all()]
    observe_latency("vector_search", (time.time() - start) * 1000)
    return rows


def _apply_filters(stmt, filters: dict):
    if filters.get("service_name"):
        stmt = stmt.where(Chunk.service_name == filters["service_name"])
    if filters.get("deploy_hash"):
        stmt = stmt.where(Chunk.deploy_hash == filters["deploy_hash"])
    if filters.get("chunk_type"):
        stmt = stmt.where(Chunk.chunk_type == filters["chunk_type"])
    if filters.get("endpoint"):
        stmt = stmt.where(Chunk.endpoint == filters["endpoint"])
    return stmt

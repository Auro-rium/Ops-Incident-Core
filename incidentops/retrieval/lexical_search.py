"""
Lexical search — Postgres full-text search over chunk tsvectors.
"""

from __future__ import annotations

import uuid
import time
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from incidentops.db.models import Chunk
from incidentops.observability.metrics import observe_latency


async def lexical_search(
    db: AsyncSession,
    project_id: uuid.UUID,
    query: str,
    top_k: int = 20,
    filters: dict[str, Any] | None = None,
) -> list[dict]:
    """
    Full-text search using ts_rank_cd with plainto_tsquery.
    Returns list of {chunk, score} dicts ordered by relevance.
    """
    start = time.time()
    tsquery = func.plainto_tsquery("english", query)
    rank = func.ts_rank_cd(Chunk.search_tsvector, tsquery)

    stmt = (
        select(Chunk, rank.label("score"))
        .options(selectinload(Chunk.document))
        .where(Chunk.project_id == project_id)
        .where(Chunk.search_tsvector.op("@@")(tsquery))
    )

    if filters:
        stmt = apply_chunk_filters(stmt, filters)

    stmt = stmt.order_by(rank.desc()).limit(top_k)
    result = await db.execute(stmt)
    rows = [{"chunk": row[0], "score": float(row[1])} for row in result.all()]
    observe_latency("lexical_search", (time.time() - start) * 1000)
    return rows


def apply_chunk_filters(stmt, filters: dict[str, Any]):
    """Apply request filters shared by the lexical, metadata, and graph branches."""
    if filters.get("service_name"):
        stmt = stmt.where(Chunk.service_name == filters["service_name"])
    if filters.get("deploy_hash"):
        stmt = stmt.where(Chunk.deploy_hash == filters["deploy_hash"])
    if filters.get("chunk_type"):
        stmt = stmt.where(Chunk.chunk_type == filters["chunk_type"])
    if filters.get("endpoint"):
        stmt = stmt.where(endpoint_filter_clause(str(filters["endpoint"])))
    return stmt


def endpoint_filter_clause(endpoint: str):
    """Match an endpoint exactly or as a path prefix (``/orders`` matches ``/orders/{id}``)."""
    base = endpoint.rstrip("/") or "/"
    escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    prefix = f"{escaped}%" if base == "/" else f"{escaped}/%"
    exact = {base, endpoint}
    return or_(*(Chunk.endpoint == value for value in sorted(exact)), Chunk.endpoint.like(prefix, escape="\\"))

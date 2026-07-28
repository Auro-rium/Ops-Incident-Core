"""Bounded exact metadata and path retrieval for hybrid search."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from incidentops.db.models import Chunk, Document

_IGNORED_TERMS = {
    "about",
    "after",
    "before",
    "does",
    "from",
    "implemented",
    "implementation",
    "investigate",
    "latency",
    "parts",
    "question",
    "relevant",
    "service",
    "that",
    "the",
    "this",
    "what",
    "when",
    "where",
    "which",
    "with",
}


async def metadata_search(
    db: AsyncSession,
    project_id: uuid.UUID,
    query_info: dict[str, Any],
    *,
    top_k: int,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Find exact service, deploy, endpoint, symbol, or path candidates.

    The branch is deliberately narrow: it is a deterministic complement to
    lexical and vector retrieval, not a fuzzy fourth search engine.
    """
    services = _normalized_strings(query_info.get("services"))
    deploy_hashes = _normalized_strings(query_info.get("deploy_hashes"))
    endpoints = _normalized_strings(query_info.get("endpoints"))
    terms = _path_terms(query_info.get("query_terms"))

    predicates = []
    if services:
        predicates.append(func.lower(Chunk.service_name).in_(services))
    if deploy_hashes:
        predicates.append(func.lower(Chunk.deploy_hash).in_(deploy_hashes))
    for endpoint in endpoints:
        predicates.append(Chunk.endpoint.ilike(f"%{endpoint}%"))
    for term in terms:
        predicates.extend(
            (
                func.lower(Chunk.metadata_json["symbol_name"].astext) == term,
                func.lower(Chunk.metadata_json["symbol"].astext) == term,
                Document.path.ilike(f"%{term}%"),
            )
        )
    if not predicates:
        return []

    stmt = (
        select(Chunk)
        .join(Document, Chunk.document_id == Document.id)
        .options(selectinload(Chunk.document))
        .where(Chunk.project_id == project_id, or_(*predicates))
        .limit(max(top_k * 3, top_k))
    )
    if filters:
        stmt = _apply_filters(stmt, filters)
    result = await db.execute(stmt)
    scored = [
        {"chunk": chunk, "score": _exact_score(chunk, services, deploy_hashes, endpoints, terms)}
        for chunk in result.scalars().all()
    ]
    return sorted(scored, key=lambda item: item["score"], reverse=True)[:top_k]


def _normalized_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).lower().strip() for item in value if isinstance(item, str) and item.strip()})


def _path_terms(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            term.lower()
            for term in value
            if isinstance(term, str) and len(term) >= 4 and term.lower() not in _IGNORED_TERMS
        }
    )[:8]


def _apply_filters(stmt, filters: dict[str, Any]):
    if filters.get("service_name"):
        stmt = stmt.where(Chunk.service_name == filters["service_name"])
    if filters.get("deploy_hash"):
        stmt = stmt.where(Chunk.deploy_hash == filters["deploy_hash"])
    if filters.get("chunk_type"):
        stmt = stmt.where(Chunk.chunk_type == filters["chunk_type"])
    if filters.get("endpoint"):
        stmt = stmt.where(Chunk.endpoint == filters["endpoint"])
    return stmt


def _exact_score(
    chunk: Chunk,
    services: list[str],
    deploy_hashes: list[str],
    endpoints: list[str],
    terms: list[str],
) -> float:
    metadata = chunk.metadata_json or {}
    symbols = {
        str(value).lower()
        for value in [metadata.get("symbol_name"), metadata.get("symbol"), *(metadata.get("symbol_names") or [])]
        if isinstance(value, str)
    }
    path = ((getattr(chunk.document, "path", "") or metadata.get("document_path") or "").lower())
    score = 0.0
    if chunk.service_name and chunk.service_name.lower() in services:
        score += 4.0
    if chunk.deploy_hash and chunk.deploy_hash.lower() in deploy_hashes:
        score += 4.0
    if chunk.endpoint and any(endpoint in chunk.endpoint.lower() for endpoint in endpoints):
        score += 5.0
    score += 3.0 * len(symbols.intersection(terms))
    score += min(2.0, float(sum(term in path for term in terms)))
    return score

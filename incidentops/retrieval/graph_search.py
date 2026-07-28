"""Project-scoped traversal of deterministic evidence relations."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from incidentops.db.models import Chunk, EvidenceRelation


async def graph_search(
    db: AsyncSession,
    project_id: uuid.UUID,
    query_info: dict[str, Any],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    """Return chunks connected to exact graph keys mentioned by a query."""
    terms = _terms(query_info)
    if not terms:
        return []
    predicates = []
    for term in terms:
        predicates.extend(
            (
                func.lower(EvidenceRelation.from_key).like(f"%{term}%"),
                func.lower(EvidenceRelation.to_key).like(f"%{term}%"),
            )
        )
    stmt = (
        select(EvidenceRelation, Chunk)
        .join(Chunk, EvidenceRelation.evidence_chunk_id == Chunk.id)
        .options(selectinload(Chunk.document))
        .where(EvidenceRelation.project_id == project_id, or_(*predicates))
        .limit(max(top_k * 4, top_k))
    )
    rows = await db.execute(stmt)
    candidates: dict[uuid.UUID, dict[str, Any]] = {}
    for relation, chunk in rows.all():
        score = _relation_score(relation, terms)
        current = candidates.get(chunk.id)
        if current is None or score > current["score"]:
            candidates[chunk.id] = {"chunk": chunk, "score": score, "relation_type": relation.relation_type}
    return sorted(candidates.values(), key=lambda item: item["score"], reverse=True)[:top_k]


def _terms(query_info: dict[str, Any]) -> list[str]:
    services = query_info.get("services", [])
    service_values = {value.lower() for value in services if isinstance(value, str)}
    values = [*services, *query_info.get("endpoints", []), *query_info.get("query_terms", [])]
    return sorted(
        {
            value.lower()
            for value in values
            if isinstance(value, str)
            and (len(value) >= 3 if value.lower() in service_values else len(value) >= 4)
            and value.lower() not in {"where", "which", "service", "architecture"}
        }
    )[:8]


def _relation_score(relation: EvidenceRelation, terms: list[str]) -> float:
    haystack = f"{relation.from_key} {relation.to_key}".lower()
    score = float(sum(term in haystack for term in terms))
    if relation.relation_type in {"defines", "implements"}:
        score += 1.0
    return score

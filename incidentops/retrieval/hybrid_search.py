"""
Hybrid search with generic entity extraction from arbitrary user queries.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.config.settings import get_settings
from incidentops.ingestion.chunking.metadata import extract_deploy_hash
from incidentops.retrieval.embeddings import embed_query
from incidentops.retrieval.lexical_search import lexical_search
from incidentops.retrieval.vector_search import vector_search

logger = logging.getLogger("incidentops.retrieval.hybrid")

DEPLOY_HASH_RE = re.compile(r"\b([a-f0-9]{6,40})\b", re.IGNORECASE)
ENDPOINT_RE = re.compile(r"(/\w[\w/-]*)")
SERVICE_RE = re.compile(r"\b([a-z][a-z0-9_-]{2,40})(?:\s+service)?\b", re.IGNORECASE)
SERVICE_STOPWORDS = {
    "why",
    "what",
    "when",
    "where",
    "who",
    "how",
    "did",
    "does",
    "after",
    "before",
    "last",
    "increase",
    "increased",
    "slow",
    "slower",
    "spike",
    "spiked",
    "latency",
    "timeout",
    "timeouts",
    "error",
    "errors",
    "rate",
    "question",
    "question",
    "after",
    "deploy",
    "release",
    "incident",
    "investigation",
    "query",
    "logs",
    "docs",
    "get",
    "post",
    "put",
    "delete",
    "patch",
}


def analyze_query(query: str) -> dict:
    lower = query.lower()
    endpoints = ENDPOINT_RE.findall(query)
    deploy_hashes = []
    explicit = extract_deploy_hash(query)
    if explicit:
        deploy_hashes.append(explicit)
    deploy_hashes.extend(
        candidate.lower()
        for candidate in DEPLOY_HASH_RE.findall(query)
        if candidate.lower() not in deploy_hashes
    )
    services = []
    for match in SERVICE_RE.findall(lower):
        candidate = match.lower()
        if candidate in SERVICE_STOPWORDS or candidate.startswith("http"):
            continue
        if candidate in endpoints or candidate in deploy_hashes:
            continue
        services.append(candidate)
    services = list(dict.fromkeys(services))[:5]

    source_hints = []
    if any(word in lower for word in ("log", "logs", "trace", "warning")):
        source_hints.append("logs")
    if any(word in lower for word in ("deploy", "release", "commit", "diff", "rollback")):
        source_hints.append("deploy")
    if any(word in lower for word in ("incident", "postmortem", "previous")):
        source_hints.append("incident")
    if any(word in lower for word in ("code", "function", "module", "stack")):
        source_hints.append("code")
    if any(word in lower for word in ("runbook", "owner", "ownership", "docs")):
        source_hints.append("runbook")

    return {
        "services": services,
        "deploy_hashes": deploy_hashes,
        "endpoints": endpoints,
        "source_hints": source_hints,
    }


async def hybrid_search(
    db: AsyncSession,
    project_id: uuid.UUID,
    query: str,
    top_k: int = 10,
    filters: dict[str, Any] | None = None,
) -> list[dict]:
    results, _ = await hybrid_search_with_debug(db, project_id, query, top_k=top_k, filters=filters)
    return results


async def hybrid_search_with_debug(
    db: AsyncSession,
    project_id: uuid.UUID,
    query: str,
    top_k: int = 10,
    filters: dict[str, Any] | None = None,
) -> tuple[list[dict], dict]:
    settings = get_settings()
    query_info = analyze_query(query)
    logger.info("Query analysis: %s", query_info)
    query_embedding = embed_query(query, settings.embedding_model)
    fetch_k = max(top_k * 3, 30)
    vec_results = await vector_search(db, project_id, query_embedding, top_k=fetch_k, filters=filters)
    lex_results = await lexical_search(db, project_id, query, top_k=fetch_k, filters=filters)

    scores: dict[uuid.UUID, dict] = {}
    vec_max = max((result["score"] for result in vec_results), default=1.0)
    for result in vec_results:
        chunk_id = result["chunk"].id
        scores[chunk_id] = {
            "chunk": result["chunk"],
            "vector_score": result["score"] / vec_max if vec_max else 0.0,
            "lexical_score": 0.0,
            "metadata_boost": 0.0,
        }

    lex_max = max((result["score"] for result in lex_results), default=1.0)
    for result in lex_results:
        chunk_id = result["chunk"].id
        lexical_score = result["score"] / lex_max if lex_max else 0.0
        if chunk_id in scores:
            scores[chunk_id]["lexical_score"] = lexical_score
        else:
            scores[chunk_id] = {
                "chunk": result["chunk"],
                "vector_score": 0.0,
                "lexical_score": lexical_score,
                "metadata_boost": 0.0,
            }

    for entry in scores.values():
        chunk = entry["chunk"]
        boost = 0.0
        boost_reasons: list[str] = []
        if chunk.deploy_hash and chunk.deploy_hash.lower() in query_info["deploy_hashes"]:
            boost += 0.15
            boost_reasons.append("deploy_hash_match")
        if chunk.service_name and chunk.service_name.lower() in query_info["services"]:
            boost += 0.1
            boost_reasons.append("service_name_match")
        if chunk.endpoint:
            for endpoint in query_info["endpoints"]:
                if endpoint in chunk.endpoint or chunk.endpoint in endpoint:
                    boost += 0.1
                    boost_reasons.append("endpoint_match")
                    break
        if _chunk_source_type(chunk) in query_info["source_hints"]:
            boost += 0.05
            boost_reasons.append("source_hint_match")
        entry["metadata_boost"] = min(boost, 0.3)
        entry["metadata_boost_reasons"] = boost_reasons
        entry["fused_score"] = (
            settings.vector_weight * entry["vector_score"]
            + settings.lexical_weight * entry["lexical_score"]
            + settings.metadata_weight * entry["metadata_boost"]
        )

    ranked_all = sorted(scores.values(), key=lambda item: item["fused_score"], reverse=True)
    ranked = ranked_all[:top_k]
    results = [
        {
            "chunk": item["chunk"],
            "vector_score": item["vector_score"],
            "lexical_score": item["lexical_score"],
            "metadata_boost": item["metadata_boost"],
            "metadata_boost_reasons": item["metadata_boost_reasons"],
            "fused_score": item["fused_score"],
        }
        for item in ranked
    ]
    debug = {
        "extracted_entities": query_info,
        "applied_filters": filters or {},
        "vector_candidates_count": len(vec_results),
        "lexical_candidates_count": len(lex_results),
        "merged_candidates_count": len(scores),
        "metadata_boosts_used": _summarize_boosts(ranked_all[: min(10, len(ranked_all))]),
        "top_rejected": _top_rejected_candidates(ranked_all, top_k),
    }
    return results, debug


def _chunk_source_type(chunk) -> str:
    chunk_type = chunk.chunk_type or ""
    if chunk_type == "log_window":
        return "logs"
    if chunk_type == "deploy_diff":
        return "deploy"
    if chunk_type == "incident_section":
        return "incident"
    if chunk_type == "function":
        return "code"
    if chunk_type == "markdown_section":
        return "runbook"
    return ""


def _summarize_boosts(ranked: list[dict]) -> list[dict]:
    summary = []
    for item in ranked:
        chunk = item["chunk"]
        reasons = item.get("metadata_boost_reasons") or []
        if not reasons:
            continue
        summary.append(
            {
                "document_path": getattr(getattr(chunk, "document", None), "path", "") or "",
                "chunk_id": str(chunk.id),
                "metadata_boost": round(item.get("metadata_boost", 0.0), 4),
                "reasons": reasons,
            }
        )
    return summary


def _top_rejected_candidates(ranked_all: list[dict], top_k: int) -> list[dict]:
    rejected = []
    for item in ranked_all[top_k : top_k + 5]:
        chunk = item["chunk"]
        reason = "below_top_k_after_score_merge"
        if item.get("fused_score", 0.0) < 0.15:
            reason = "low_fused_score"
        rejected.append(
            {
                "chunk_id": str(chunk.id),
                "document_path": getattr(getattr(chunk, "document", None), "path", "") or "",
                "source_type": _chunk_source_type(chunk),
                "score": round(item.get("fused_score", 0.0), 4),
                "reason": reason,
            }
        )
    return rejected

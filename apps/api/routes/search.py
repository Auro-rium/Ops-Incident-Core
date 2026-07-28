from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import check_rate_limit, enforce_query_limits, ensure_project_access, get_current_user, get_db, get_settings_dep
from incidentops.config.settings import Settings
from incidentops.observability.metrics import incr, observe_latency
from incidentops.operations.service import record_operational_event
from incidentops.retrieval.citation_builder import build_citations
from incidentops.retrieval.evidence_packer import pack_evidence
from incidentops.retrieval.hybrid_search import hybrid_search, hybrid_search_with_debug
from incidentops.retrieval.query_intent import classify_query_intent
from incidentops.retrieval.reranker import rerank_with_debug_async
from incidentops.schemas.api import CitationInfo, SearchHit, SearchRequest, SearchResponse

router = APIRouter(prefix="/v1", tags=["Search"])


@router.post("/search", response_model=SearchResponse)
async def search(
    body: SearchRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    enforce_query_limits(body.query, body.top_k, settings)
    if user:
        await check_rate_limit(
            db,
            settings,
            f"search:{user.id}",
            settings.user_request_limit,
            settings.rate_limit_window_seconds,
            user=user,
            project_id=body.project_id,
            action="search",
        )
    await ensure_project_access(db, body.project_id, user, settings)
    start = time.time()
    query_intent = classify_query_intent(body.query)
    if body.debug:
        raw_results, retrieval_debug = await hybrid_search_with_debug(
            db,
            body.project_id,
            body.query,
            top_k=max(body.top_k * 3, 30),
            filters=body.filters,
        )
    else:
        raw_results = await hybrid_search(
            db, body.project_id, body.query, top_k=max(body.top_k * 3, 30), filters=body.filters
        )
        retrieval_debug = None
    reranked, rerank_debug = await rerank_with_debug_async(
        body.query,
        raw_results,
        model_name=settings.reranker_model,
        top_k=min(body.top_k, settings.max_retrieved_chunks),
    )
    evidence = pack_evidence(reranked, max_evidence=min(body.top_k, settings.max_retrieved_chunks))
    build_citations(evidence)
    latency_ms = int((time.time() - start) * 1000)
    incr("search_requests")
    incr("search_requests_total")
    if not evidence:
        incr("zero_result_total")
    observe_latency("search", latency_ms)
    observe_latency("search_latency", latency_ms)
    hits = [
        SearchHit(
            chunk_id=item["chunk_id"],
            rank=i + 1,
            source_type=item["source_type"],
            document_path=item.get("document_path", ""),
            service_name=item.get("service_name"),
            deploy_hash=item.get("deploy_hash"),
            endpoint=item.get("endpoint"),
            score=round(item.get("score", 0), 4),
            text_preview=item["text"][:300],
            citation=CitationInfo(**item["citation"]),
        )
        for i, item in enumerate(evidence)
    ]
    debug = None
    if body.debug:
        debug = retrieval_debug or {}
        debug["reranked_count"] = len(reranked)
        debug["rerank"] = rerank_debug
    evidence_mix = {
        "source_types": _count_values(item["source_type"] for item in evidence),
        "chunk_types": _count_values(item.get("chunk_type") or "unknown" for item in evidence),
    }
    await record_operational_event(
        db,
        project_id=body.project_id,
        category="retrieval",
        event_type="search_completed",
        severity="medium" if not evidence else "info",
        payload={
            "query_intent": query_intent.intent,
            "result_count": len(hits),
            "latency_ms": latency_ms,
            "source_type_distribution": evidence_mix["source_types"],
            "chunk_type_distribution": evidence_mix["chunk_types"],
            "rerank_mode": (rerank_debug or {}).get("mode"),
        },
    )
    return SearchResponse(
        query=body.query,
        results=hits,
        total=len(hits),
        latency_ms=latency_ms,
        query_intent=query_intent.intent,
        evidence_mix=evidence_mix,
        debug=debug,
    )


def _count_values(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts

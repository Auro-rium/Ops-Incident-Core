from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import check_rate_limit, enforce_query_limits, ensure_project_access, get_current_user, get_db, get_settings_dep
from incidentops.config.settings import Settings
from incidentops.observability.metrics import incr, observe_latency
from incidentops.retrieval.citation_builder import build_citations
from incidentops.retrieval.evidence_packer import pack_evidence
from incidentops.retrieval.hybrid_search import hybrid_search, hybrid_search_with_debug
from incidentops.retrieval.reranker import rerank
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
    reranked = rerank(body.query, raw_results, model_name=settings.reranker_model, top_k=min(body.top_k, settings.max_retrieved_chunks))
    evidence = pack_evidence(reranked, max_evidence=min(body.top_k, settings.max_retrieved_chunks))
    build_citations(evidence)
    latency_ms = int((time.time() - start) * 1000)
    incr("search_requests")
    observe_latency("search", latency_ms)
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
    return SearchResponse(query=body.query, results=hits, total=len(hits), latency_ms=latency_ms, debug=debug)

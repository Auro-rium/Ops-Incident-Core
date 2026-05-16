from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import check_rate_limit, enforce_query_limits, ensure_project_access, get_current_user, get_db, get_settings_dep
from incidentops.config.settings import Settings
from incidentops.db.models import ProjectRole
from incidentops.llm.prompts import build_answer_prompt
from incidentops.llm.provider import get_llm_provider
from incidentops.observability.metrics import incr, observe_latency
from incidentops.retrieval.citation_builder import build_citations
from incidentops.retrieval.evidence_packer import pack_evidence
from incidentops.retrieval.hybrid_search import hybrid_search
from incidentops.retrieval.reranker import rerank
from incidentops.schemas.api import (
    AnswerBody,
    AnswerCitation,
    AnswerRequest,
    AnswerResponse,
    CitationInfo,
    EvidenceItem,
)
from incidentops.security.output_sanitizer import sanitize_output

router = APIRouter(prefix="/v1", tags=["Answer"])


@router.post("/answer", response_model=AnswerResponse)
async def answer(
    body: AnswerRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    enforce_query_limits(body.query, body.top_k, settings)
    if user:
        await check_rate_limit(
            db,
            settings,
            f"answer:{user.id}",
            settings.user_request_limit,
            settings.rate_limit_window_seconds,
            user=user,
            project_id=body.project_id,
            action="answer",
        )
    await ensure_project_access(db, body.project_id, user, settings, minimum_role=ProjectRole.investigator)
    start = time.time()
    raw_results = await hybrid_search(db, body.project_id, body.query, top_k=max(body.top_k * 3, 30))
    reranked = rerank(body.query, raw_results, model_name=settings.reranker_model, top_k=body.top_k)
    evidence = pack_evidence(reranked, max_evidence=body.top_k)
    build_citations(evidence)
    evidence_items = [
        EvidenceItem(
            chunk_id=item["chunk_id"],
            source_type=item["source_type"],
            document_path=item.get("document_path", ""),
            service_name=item.get("service_name"),
            deploy_hash=item.get("deploy_hash"),
            score=round(item.get("score", 0), 4),
            text_preview=item["text"][:300],
            citation=CitationInfo(**item["citation"]),
        )
        for item in evidence
    ]
    llm = get_llm_provider()
    answer_body = None
    message = None
    if llm.available:
        messages = build_answer_prompt(body.query, evidence)
        llm_response = await llm.chat(messages, temperature=0.1, max_tokens=1024, response_format={"type": "json_object"})
        if llm_response and "raw_response" not in llm_response:
            answer_body = AnswerBody(
                summary=sanitize_output(llm_response.get("summary", "")),
                likely_root_cause=sanitize_output(llm_response.get("likely_root_cause", "")),
                confidence=llm_response.get("confidence", "unknown"),
                affected_services=llm_response.get("affected_services", []),
                reasoning=sanitize_output(llm_response.get("reasoning", "")) if llm_response.get("reasoning") else None,
                suggested_fix=sanitize_output(llm_response.get("suggested_fix", "")) if llm_response.get("suggested_fix") else None,
                unknowns=llm_response.get("unknowns"),
                citations=[
                    AnswerCitation(
                        label=item["citation"]["label"],
                        document_path=item.get("document_path", ""),
                        lines=item["citation"]["lines"],
                        chunk_id=item["chunk_id"],
                    )
                    for item in evidence
                ],
            )
    else:
        message = "LLM provider not configured. Returning retrieved evidence only."
    latency_ms = int((time.time() - start) * 1000)
    incr("answer_requests")
    incr("answer_requests_total")
    observe_latency("answer", latency_ms)
    observe_latency("answer_latency", latency_ms)
    return AnswerResponse(
        question=body.query,
        answer=answer_body,
        message=message,
        evidence=evidence_items,
        latency_ms=latency_ms,
    )

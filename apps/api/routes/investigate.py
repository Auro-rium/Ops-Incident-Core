from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import ensure_project_access, get_current_user, get_db, get_settings_dep
from incidentops.config.settings import Settings
from incidentops.db.models import Project
from incidentops.investigation.service import investigate as run_investigation
from incidentops.schemas.api import (
    CitationInfo,
    EvidenceItem,
    HypothesisResponse,
    InvestigationEntityResponse,
    InvestigationRequest,
    InvestigationResponse,
    RootCauseResponse,
    TimelineEventResponse,
)
from incidentops.security.rate_limit import limiter

router = APIRouter(prefix="/v1", tags=["Investigate"])


@router.post("/investigate", response_model=InvestigationResponse)
async def investigate(
    body: InvestigationRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    if user:
        limiter.check(f"investigate:{user.id}", settings.user_request_limit, 60)
    await ensure_project_access(db, body.project_id, user, settings)
    investigation, latency_ms = await run_investigation(
        db,
        body.project_id,
        body.query[: settings.max_query_length],
        min(body.top_k, settings.max_retrieved_chunks),
        settings.reranker_model,
        debug=body.debug,
    )
    return InvestigationResponse(
        question=investigation.question,
        task_type=investigation.task_type,
        entities=InvestigationEntityResponse(
            service_name=investigation.entities.service_name,
            endpoint=investigation.entities.endpoint,
            deploy_hash=investigation.entities.deploy_hash,
            symptom=investigation.entities.symptom,
            time_window=investigation.entities.time_window,
        ),
        timeline=[
            TimelineEventResponse(
                ts=event.ts.isoformat() if event.ts else None,
                event_type=event.event_type,
                summary=event.summary,
                evidence_chunk_ids=event.evidence_chunk_ids,
            )
            for event in investigation.timeline
        ],
        hypotheses=[
            HypothesisResponse(
                hypothesis_id=h.hypothesis_id,
                summary=h.summary,
                score=h.score,
                confidence=h.confidence,
                evidence_chunk_ids=h.evidence_chunk_ids,
                supporting_reasons=h.supporting_reasons,
                contradicting_reasons=h.contradicting_reasons,
            )
            for h in investigation.hypotheses
        ],
        likely_root_cause=RootCauseResponse(
            hypothesis_id=investigation.likely_root_cause.hypothesis_id,
            summary=investigation.likely_root_cause.summary,
            confidence=investigation.likely_root_cause.confidence,
            evidence_chunk_ids=investigation.likely_root_cause.evidence_chunk_ids,
        ),
        confidence=investigation.confidence,
        confidence_reasons=investigation.confidence_reasons,
        affected_services=investigation.affected_services,
        suggested_fix=investigation.suggested_fix,
        citations=[CitationInfo(**citation) for citation in investigation.citations],
        missing_data=investigation.missing_data,
        unknowns=investigation.unknowns,
        evidence=[
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
            for item in investigation.evidence
        ],
        latency_ms=latency_ms,
        debug=investigation.debug or None,
    )

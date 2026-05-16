from __future__ import annotations

import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.investigation.classifier import classify_task
from incidentops.investigation.deploy_analyzer import analyze_deploy_diff
from incidentops.investigation.entity_extractor import extract_entities
from incidentops.investigation.hypothesis_generator import generate_hypotheses
from incidentops.investigation.incident_matcher import find_previous_incidents
from incidentops.investigation.log_analyzer import analyze_logs
from incidentops.investigation.root_cause_selector import select_root_cause
from incidentops.investigation.schemas import InvestigationResult
from incidentops.investigation.scope_resolver import resolve_scope
from incidentops.investigation.timeline_builder import build_timeline
from incidentops.observability.metrics import incr, observe_latency
from incidentops.retrieval.citation_builder import build_citations
from incidentops.retrieval.evidence_packer import pack_evidence
from incidentops.retrieval.hybrid_search import hybrid_search, hybrid_search_with_debug
from incidentops.retrieval.reranker import rerank


async def investigate(
    db: AsyncSession,
    project_id: uuid.UUID,
    query: str,
    top_k: int,
    reranker_model: str,
    debug: bool = False,
) -> tuple[InvestigationResult, int]:
    start = time.time()
    task_type = classify_task(query)
    entities = extract_entities(query)
    scope = resolve_scope(entities)
    if debug:
        raw_results, retrieval_debug = await hybrid_search_with_debug(
            db,
            project_id,
            query,
            top_k=max(top_k * 3, 30),
            filters=scope["filters"] or None,
        )
    else:
        raw_results = await hybrid_search(
            db,
            project_id,
            query,
            top_k=max(top_k * 3, 30),
            filters=scope["filters"] or None,
        )
        retrieval_debug = {}
    reranked = rerank(query, raw_results, model_name=reranker_model, top_k=top_k)
    evidence = pack_evidence(reranked, max_evidence=top_k)
    build_citations(evidence)

    log_findings = analyze_logs(evidence)
    deploy_findings = analyze_deploy_diff(evidence, entities.deploy_hash)
    previous_incidents = find_previous_incidents(
        evidence,
        {"service_name": entities.service_name, "symptom": entities.symptom},
    )
    hypotheses = generate_hypotheses(entities, evidence, log_findings, deploy_findings, previous_incidents)
    selected = select_root_cause(hypotheses, evidence)
    timeline = build_timeline(evidence, log_findings, deploy_findings, previous_incidents)

    affected_services = sorted(
        {
            service
            for service in [entities.service_name, *log_findings["services"]]
            if service
        }
    )
    missing_data = []
    if not deploy_findings["diff_count"]:
        missing_data.append("deployment history not found")
    if not log_findings["timestamped_logs"]:
        missing_data.append("logs missing timestamps")
    if not previous_incidents:
        missing_data.append("no previous incident reports")
    if not any(item.get("source_type") == "deploy" for item in evidence):
        missing_data.append("no code diff found")
    if not any(item.get("source_type") in {"runbook", "api_doc"} for item in evidence):
        missing_data.append("no service ownership docs")

    confidence, confidence_reasons = _score_confidence(
        entities=entities,
        evidence=evidence,
        log_findings=log_findings,
        deploy_findings=deploy_findings,
        previous_incidents=previous_incidents,
        missing_data=missing_data,
    )
    selected.confidence = confidence

    suggested_fix = None
    if confidence != "low":
        suggested_fix = "Validate the leading hypothesis against deploy history, recent logs, and ownership documentation before taking action."

    result = InvestigationResult(
        question=query,
        task_type=task_type,
        entities=entities,
        timeline=timeline,
        hypotheses=hypotheses,
        likely_root_cause=selected,
        confidence=confidence,
        confidence_reasons=confidence_reasons,
        affected_services=affected_services,
        suggested_fix=suggested_fix,
        citations=[item["citation"] for item in evidence],
        missing_data=missing_data,
        unknowns=missing_data,
        evidence=evidence,
        debug={
            **retrieval_debug,
            "applied_filters": scope["filters"] or {},
            "reranked_count": len(reranked),
        }
        if debug
        else {},
    )
    latency_ms = int((time.time() - start) * 1000)
    incr("investigations_total")
    if confidence == "low":
        incr("weak_evidence_total")
    observe_latency("investigation", latency_ms)
    return result, latency_ms


def _score_confidence(
    *,
    entities,
    evidence: list[dict],
    log_findings: dict,
    deploy_findings: dict,
    previous_incidents: list[dict],
    missing_data: list[str],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    source_types = {item.get("source_type") for item in evidence if item.get("source_type")}
    strong_deploy_match = bool(
        entities.deploy_hash
        and any(item.get("deploy_hash") == entities.deploy_hash for item in evidence)
    )
    strong_endpoint_match = bool(
        entities.endpoint
        and any(item.get("endpoint") == entities.endpoint for item in evidence if item.get("endpoint"))
    )
    has_logs = "logs" in source_types
    has_code = "code" in source_types
    has_deploys = "deploy" in source_types

    if len(source_types) >= 3:
        reasons.append("multiple source types agree on relevant context")
    if strong_deploy_match:
        reasons.append("deploy hash matches retrieved evidence")
    if strong_endpoint_match:
        reasons.append("endpoint match found in retrieved evidence")
    if log_findings.get("timestamped_logs"):
        reasons.append("timestamped logs support ordering")
    if previous_incidents:
        reasons.append("previous incident evidence is available for comparison")

    if len(source_types) >= 3 and strong_deploy_match and has_logs and (has_code or has_deploys):
        return "high", reasons
    if evidence and len(source_types) >= 2 and len(missing_data) <= 2:
        if not reasons:
            reasons.append("relevant evidence exists but one key source is missing")
        return "medium", reasons
    if not reasons:
        reasons.append("retrieval is weak or missing key deploy/log/code context")
    if len(missing_data) > 2:
        reasons.append("multiple key evidence sources are missing")
    return "low", reasons

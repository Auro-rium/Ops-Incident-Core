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
from incidentops.retrieval.query_intent import (
    INTENT_API_CONTRACT,
    INTENT_CODE_LOCATION,
    INTENT_CONFIG_LOOKUP,
    INTENT_DEPLOY_REGRESSION,
    INTENT_PREVIOUS_INCIDENT,
    INTENT_RUNTIME_INCIDENT,
    classify_query_intent,
    investigate_supported,
)
from incidentops.retrieval.reranker import rerank_with_debug_async
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
    query_intent = classify_query_intent(body.query)
    raw_results = await hybrid_search(db, body.project_id, body.query, top_k=max(body.top_k * 3, 30))
    rerank_limit = max(body.top_k * 3, settings.answer_max_evidence_chunks * 2)
    reranked, rerank_debug = await rerank_with_debug_async(
        body.query,
        raw_results,
        model_name=settings.reranker_model,
        top_k=rerank_limit,
    )
    evidence = pack_evidence(
        reranked,
        max_evidence=min(body.top_k, settings.answer_max_evidence_chunks),
        max_chars_per_chunk=settings.answer_max_chars_per_chunk,
        max_total_chars=settings.answer_max_total_chars,
    )
    build_citations(evidence)
    evidence_items = _to_evidence_items(evidence)
    llm = get_llm_provider()
    answer_body = None
    message = None
    llm_usage = None
    llm_latency_ms = None
    synthesis_mode = "retrieval_only"
    warnings = _answer_warnings(query_intent.intent, evidence, rerank_debug)
    supported, support_reasons = investigate_supported(
        query_intent,
        {item.get("source_type") for item in evidence if item.get("source_type")},
    )
    if query_intent.is_incident_like and not supported:
        answer_body = _insufficient_evidence_answer(body.query, evidence, warnings + support_reasons)
        synthesis_mode = "insufficient_evidence"
    elif direct_evidence := _pack_direct_lookup_evidence(query_intent.intent, reranked, settings):
        evidence = direct_evidence
        build_citations(evidence)
        evidence_items = _to_evidence_items(evidence)
        answer_body = _build_direct_answer(query_intent.intent, evidence)
        synthesis_mode = "direct_evidence"
    elif llm.available:
        messages = build_answer_prompt(body.query, evidence, query_intent=query_intent.intent, warnings=warnings)
        llm_response = await llm.chat(messages, temperature=0.1, max_tokens=1024, response_format={"type": "json_object"})
        if llm_response and "raw_response" not in llm_response:
            llm_meta = llm_response.get("_meta") or {}
            llm_usage = {str(k): int(v) for k, v in (llm_meta.get("usage") or {}).items() if isinstance(v, int | float)}
            llm_latency_ms = int(llm_meta.get("latency_ms", 0) or 0) or None
            answer_body = AnswerBody(
                summary=sanitize_output(llm_response.get("summary", "")),
                likely_root_cause=sanitize_output(llm_response.get("likely_root_cause", "")),
                confidence=llm_response.get("confidence", "unknown"),
                affected_services=llm_response.get("affected_services", []),
                answer_text=sanitize_output(llm_response.get("answer_text", "")) if llm_response.get("answer_text") else None,
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
            synthesis_mode = "llm_synthesis"
    else:
        message = "LLM provider not configured. Returning retrieved evidence only."
    latency_ms = int((time.time() - start) * 1000)
    incr("answer_requests")
    incr("answer_requests_total")
    observe_latency("answer", latency_ms)
    observe_latency("answer_latency", latency_ms)
    return AnswerResponse(
        question=body.query,
        query_intent=query_intent.intent,
        answer=answer_body,
        message=message,
        evidence=evidence_items,
        synthesis_mode=synthesis_mode,
        llm_usage=llm_usage,
        llm_latency_ms=llm_latency_ms,
        latency_ms=latency_ms,
        warnings=warnings,
    )


def _answer_warnings(query_intent: str, evidence: list[dict], rerank_debug: dict | None = None) -> list[str]:
    source_types = {item.get("source_type") for item in evidence if item.get("source_type")}
    warnings: list[str] = []
    if rerank_debug and rerank_debug.get("mode"):
        warnings.append(f"rerank_mode={rerank_debug['mode']}")
    if query_intent in {INTENT_RUNTIME_INCIDENT, INTENT_DEPLOY_REGRESSION, INTENT_PREVIOUS_INCIDENT}:
        if "logs" not in source_types:
            warnings.append("runtime logs are missing from the retrieved evidence")
        if "deploy" not in source_types:
            warnings.append("deploy history or diffs are missing from the retrieved evidence")
        if "incident" not in source_types:
            warnings.append("previous incident reports are missing from the retrieved evidence")
    return warnings


def _insufficient_evidence_answer(question: str, evidence: list[dict], warnings: list[str]) -> AnswerBody:
    services = sorted({item.get("service_name") for item in evidence if item.get("service_name")})
    return AnswerBody(
        summary="Operational evidence is insufficient to answer this incident question confidently.",
        likely_root_cause="The current evidence is missing key runtime, deploy, or incident context, so a confident root-cause explanation is not supported.",
        confidence="low",
        affected_services=services,
        reasoning="The retrieved evidence does not include enough operational context to justify a causal incident explanation. Use the cited evidence for repo context and add the missing operational evidence before drawing RCA conclusions.",
        suggested_fix="Add timestamped logs, deploy history or diffs, and previous incident or runbook evidence before using answer synthesis for runtime RCA.",
        unknowns=list(dict.fromkeys(warnings)),
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


def _build_direct_answer(query_intent: str, evidence: list[dict]) -> AnswerBody | None:
    if query_intent not in {INTENT_CODE_LOCATION, INTENT_CONFIG_LOOKUP, INTENT_API_CONTRACT} or not evidence:
        return None
    snippets = [
        _direct_snippet(item, max_chars=900)
        for item in evidence[:2]
    ]
    snippets = [snippet for snippet in snippets if snippet]
    if not snippets:
        return None
    joined = "\n\n".join(snippets)
    if len(joined) > 4500:
        joined = joined[:4500].rstrip()
    summary = {
        INTENT_CODE_LOCATION: "Direct code evidence found for this implementation lookup.",
        INTENT_CONFIG_LOOKUP: "Direct configuration evidence found for this lookup.",
        INTENT_API_CONTRACT: "Direct API contract evidence found for this lookup.",
    }[query_intent]
    likely_root_cause = {
        INTENT_CODE_LOCATION: "The top cited files show where this behavior or symbol is implemented.",
        INTENT_CONFIG_LOOKUP: "The top cited files show the configuration or deployment settings relevant to this lookup.",
        INTENT_API_CONTRACT: "The top cited files show the API contract definition relevant to this lookup.",
    }[query_intent]
    return AnswerBody(
        summary=summary,
        likely_root_cause=likely_root_cause,
        confidence="high",
        affected_services=sorted({item.get("service_name") for item in evidence if item.get("service_name")}),
        answer_text=sanitize_output(joined),
        reasoning="A direct evidence answer was returned because the highest-ranked retrieved chunks matched the requested code, config, or API location without needing broader synthesis.",
        suggested_fix=None,
        unknowns=None,
        citations=[
            AnswerCitation(
                label=item["citation"]["label"],
                document_path=item.get("document_path", ""),
                lines=item["citation"]["lines"],
                chunk_id=item["chunk_id"],
            )
            for item in evidence[: min(len(evidence), 2)]
        ],
    )


def _pack_direct_lookup_evidence(query_intent: str, results: list[dict], settings: Settings) -> list[dict]:
    if query_intent not in {INTENT_CODE_LOCATION, INTENT_CONFIG_LOOKUP, INTENT_API_CONTRACT}:
        return []
    direct_results = [result for result in results if _is_direct_lookup_candidate(query_intent, result)]
    if not direct_results:
        return []
    return pack_evidence(
        direct_results,
        max_evidence=min(2, settings.answer_max_evidence_chunks),
        max_chars_per_chunk=settings.direct_answer_max_chars_per_chunk,
        max_total_chars=settings.direct_answer_max_total_chars,
    )


def _is_direct_lookup_candidate(query_intent: str, result: dict) -> bool:
    chunk = result["chunk"]
    metadata = chunk.metadata_json or {}
    source_type = str(metadata.get("source_type") or "")
    chunk_type = str(chunk.chunk_type or "")
    path = str(getattr(getattr(chunk, "document", None), "path", "") or metadata.get("document_path") or "").lower()
    if query_intent == INTENT_CODE_LOCATION:
        return (
            source_type in {"code", "api_doc"}
            or chunk_type
            in {
                "go_function",
                "go_method",
                "go_type",
                "go_module",
                "function",
                "class",
                "module",
                "python_function",
                "python_class",
                "ts_function",
                "ts_class",
                "java_function",
                "java_class",
                "proto_service",
                "proto_rpc",
                "proto_message",
            }
            or path.endswith((".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".java", ".proto"))
        )
    if query_intent == INTENT_CONFIG_LOOKUP:
        return source_type in {"config", "deploy", "runbook", "code"} or path.endswith(
            (".yaml", ".yml", ".json", ".toml", ".ini", ".env")
        )
    return source_type in {"api_doc", "code"} or chunk_type in {
        "api_endpoint",
        "proto_service",
        "proto_rpc",
        "proto_message",
        "proto_enum",
        "proto_preamble",
    }


def _to_evidence_items(evidence: list[dict]) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            chunk_id=item["chunk_id"],
            source_type=item["source_type"],
            chunk_type=item.get("chunk_type"),
            document_path=item.get("document_path", ""),
            service_name=item.get("service_name"),
            deploy_hash=item.get("deploy_hash"),
            score=round(item.get("score", 0), 4),
            text_preview=item["text"][:300],
            why_retrieved=item.get("why_retrieved") or [],
            citation=CitationInfo(**item["citation"]),
        )
        for item in evidence
    ]


def _direct_snippet(item: dict, *, max_chars: int) -> str:
    path = item.get("document_path", "unknown")
    lines = item.get("citation", {}).get("lines", "")
    prefix = f"{path}"
    if lines:
        prefix = f"{prefix}:{lines}"
    text = (item.get("text") or "").strip()
    if len(text) > max_chars:
        text = text[: max(max_chars - 3, 0)].rstrip()
        if text:
            text = f"{text}..."
    if not text:
        return ""
    return f"{prefix}\n{text}"

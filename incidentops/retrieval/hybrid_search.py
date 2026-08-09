"""Intent-aware hybrid search with metadata-driven score fusion."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections import Counter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.config.settings import get_settings
from incidentops.ingestion.chunking.metadata import extract_deploy_hash
from incidentops.retrieval.embeddings import embed_query_async
from incidentops.retrieval.cache import get_project_json, set_project_json
from incidentops.retrieval.graph_search import graph_search
from incidentops.retrieval.lexical_search import lexical_search
from incidentops.retrieval.metadata_search import metadata_search
from incidentops.observability.metrics import incr, observe_latency
from incidentops.observability.tracing import traced
from incidentops.retrieval.query_intent import (
    INTENT_API_CONTRACT,
    INTENT_ARCHITECTURE,
    INTENT_CODE_LOCATION,
    INTENT_CONFIG_LOOKUP,
    INTENT_DEPLOY_REGRESSION,
    INTENT_GENERIC,
    INTENT_PREVIOUS_INCIDENT,
    INTENT_RUNTIME_INCIDENT,
    INTENT_RUNBOOK_LOOKUP,
    QueryIntent,
    classify_query_intent,
)
from incidentops.retrieval.vector_search import vector_search

logger = logging.getLogger("incidentops.retrieval.hybrid")

DEPLOY_HASH_RE = re.compile(r"\b([a-f0-9]{6,40})\b", re.IGNORECASE)
ENDPOINT_RE = re.compile(r"(/\w[\w/-]*)")
SERVICE_RE = re.compile(r"\b([a-z][a-z0-9_-]{2,40})(?:\s+service)?\b", re.IGNORECASE)
SERVICE_STOPWORDS = {
    "a",
    "an",
    "are",
    "application",
    "app",
    "backend",
    "configured",
    "configuration",
    "database",
    "defined",
    "deployment",
    "deployments",
    "fastapi",
    "settings",
    "used",
    "the",
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
    "deploy",
    "release",
    "incident",
    "investigation",
    "query",
    "logs",
    "docs",
    "config",
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "which",
    "parts",
    "repo",
}
README_PATH_TOKENS = ("readme.md", "/readme.md")

_SOURCE_TYPE_ALIASES = {
    "deploy_history": "deploy",
    "patch": "deploy",
    "incident_report": "incident",
    "markdown": "runbook",
    "unknown": "unknown_text",
}
_CHUNK_TYPE_BOOSTS = {
    "go_function": 0.14,
    "go_method": 0.14,
    "go_type": 0.12,
    "go_struct": 0.12,
    "go_interface": 0.12,
    "go_module": 0.08,
    "python_function": 0.14,
    "python_class": 0.12,
    "ts_function": 0.14,
    "ts_class": 0.12,
    "ts_interface": 0.12,
    "java_method": 0.14,
    "java_class": 0.12,
    "java_interface": 0.12,
    "function": 0.12,
    "class": 0.12,
    "module": 0.08,
    "code_file": 0.05,
    "go_fallback": 0.04,
    "python_fallback": 0.04,
    "ts_fallback": 0.04,
    "js_fallback": 0.04,
    "java_fallback": 0.04,
    "config_section": 0.12,
    "config_service_block": 0.14,
    "env_var_block": 0.12,
    "dependency_block": 0.12,
    "api_endpoint": 0.10,
    "openapi_endpoint": 0.14,
    "openapi_schema": 0.10,
    "proto_service": 0.12,
    "proto_rpc": 0.14,
    "proto_message": 0.08,
    "proto_enum": 0.08,
    "markdown_section": 0.08,
    "markdown_heading_section": 0.08,
    "markdown_procedure": 0.12,
    "markdown_table": 0.08,
    "markdown_faq": 0.08,
    "log_window": 0.12,
    "log_time_window": 0.12,
    "error_cluster": 0.14,
    "log_error_burst": 0.14,
    "deploy_diff": 0.12,
    "deploy_commit": 0.12,
    "incident_section": 0.12,
    "incident_symptom": 0.12,
    "incident_timeline": 0.12,
    "incident_root_cause": 0.14,
    "incident_action": 0.12,
    "release_note": 0.10,
}


def analyze_query(query: str) -> dict[str, Any]:
    lower = query.lower()
    endpoints = ENDPOINT_RE.findall(query)
    deploy_hashes: list[str] = []
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
    intent = classify_query_intent(query)
    return {
        "services": list(dict.fromkeys(services))[:5],
        "deploy_hashes": deploy_hashes,
        "endpoints": endpoints,
        "query_intent": intent.as_dict(),
        "query_terms": intent.query_terms,
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
    started = time.perf_counter()
    branch_latencies: dict[str, int] = {}
    query_info = await get_project_json("query_classification", project_id, query)
    if query_info is None:
        query_info = analyze_query(query)
        await set_project_json("query_classification", project_id, query, query_info)
    intent = classify_query_intent(query)
    incr(f"retrieval_intent_{intent.intent}_total")
    logger.info("query classified intent=%s", intent.intent)
    # Exact repository lookups are faster and more precise through lexical and
    # metadata branches; remote embeddings add latency without helping a query
    # that already names a path, symbol, config key, or API.
    skip_vector = intent.intent in {INTENT_CODE_LOCATION, INTENT_CONFIG_LOOKUP, INTENT_API_CONTRACT}
    if skip_vector:
        query_embedding = []
        branch_latencies["query_embedding_ms"] = 0
        incr("retrieval_vector_skipped_exact_lookup_total")
    else:
        embed_started = time.perf_counter()
        with traced("retrieval.query_embedding"):
            query_embedding = await embed_query_async(query, settings.embedding_model)
        branch_latencies["query_embedding_ms"] = _elapsed_ms(embed_started)
    fetch_k = _fetch_budget(top_k, intent)
    branches, branch_errors = await _retrieve_branches(
        db,
        project_id,
        query,
        query_embedding,
        query_info,
        intent,
        fetch_k,
        filters,
        parallel=settings.rag_parallel_retrieval,
        timeout_seconds=settings.rag_branch_timeout_seconds,
        branch_latencies=branch_latencies,
    )
    # Exact lookup routing is intentionally cheap, but it must not become a
    # recall cliff when lexical metadata has no useful term overlap. Fall back
    # to one bounded vector lookup only when every non-vector branch is empty.
    if _should_vector_fallback(skip_vector, branches):
        embed_started = time.perf_counter()
        try:
            with traced("retrieval.query_embedding_fallback"):
                query_embedding = await asyncio.wait_for(
                    embed_query_async(query, settings.embedding_model),
                    timeout=max(settings.rag_branch_timeout_seconds, 0.1),
                )
            branch_latencies["query_embedding_ms"] = _elapsed_ms(embed_started)
            vector_started = time.perf_counter()
            branches["vector"] = await asyncio.wait_for(
                vector_search(db, project_id, query_embedding, top_k=fetch_k, filters=filters),
                timeout=max(settings.rag_branch_timeout_seconds, 0.1),
            )
            branch_latencies["vector_fallback_search_ms"] = _elapsed_ms(vector_started)
            branch_latencies["vector_fallback"] = 1
            incr("retrieval_vector_fallback_total")
        except Exception as exc:
            branch_errors["vector_fallback"] = _safe_branch_error(exc)
            logger.warning("retrieval vector fallback failed error=%s", branch_errors["vector_fallback"])
    vec_results = branches["vector"]
    lex_results = branches["lexical"]
    metadata_results = branches["metadata"]
    graph_results = branches["graph"]

    fusion_started = time.perf_counter()
    with traced("retrieval.score_fusion"):
        scores = _weighted_rrf(
            vector_results=vec_results,
            lexical_results=lex_results,
            metadata_results=metadata_results,
            graph_results=graph_results,
            vector_weight=settings.vector_weight,
            lexical_weight=settings.lexical_weight,
            metadata_weight=settings.metadata_weight,
            graph_weight=settings.rag_graph_weight,
            rrf_k=settings.rag_rrf_k,
        )
    for entry in scores.values():
        boost, reasons = _metadata_boost(entry["chunk"], query_info, intent)
        entry["metadata_boost"] = min(boost, 0.45)
        entry["metadata_boost_reasons"] = reasons
        entry["fused_score"] = entry["rrf_score"] + entry["metadata_boost"]
    ranked_all = sorted(scores.values(), key=lambda item: item["fused_score"], reverse=True)
    branch_latencies["score_fusion_ms"] = _elapsed_ms(fusion_started)
    ranked = ranked_all[:top_k]
    results = [
        {
            "chunk": item["chunk"],
            "vector_score": item["vector_score"],
            "lexical_score": item["lexical_score"],
            "metadata_score": item["metadata_score"],
            "rrf_score": item["rrf_score"],
            "metadata_boost": item["metadata_boost"],
            "metadata_boost_reasons": item["metadata_boost_reasons"],
            "fused_score": item["fused_score"],
        }
        for item in ranked
    ]
    evidence_mix = _evidence_mix(ranked)
    for source_type, count in evidence_mix["source_types"].items():
        incr(f"retrieval_source_type_{source_type}_total", count)
    for chunk_type, count in evidence_mix["chunk_types"].items():
        incr(f"retrieval_chunk_type_{chunk_type}_total", count)
    total_latency_ms = _elapsed_ms(started)
    observe_latency("retrieval_total", total_latency_ms)
    debug = {
        "query_intent": intent.as_dict(),
        "extracted_entities": query_info,
        "applied_filters": filters or {},
        "retrieval_budget": _retrieval_budget_summary(intent, fetch_k),
        "vector_candidates_count": len(vec_results),
        "lexical_candidates_count": len(lex_results),
        "metadata_candidates_count": len(metadata_results),
        "graph_candidates_count": len(graph_results),
        "merged_candidates_count": len(scores),
        "source_type_distribution": evidence_mix["source_types"],
        "chunk_type_distribution": evidence_mix["chunk_types"],
        "applied_boosts": _reason_counts(ranked, positive=True),
        "applied_penalties": _reason_counts(ranked, positive=False),
        "retrieval_branch_latencies": branch_latencies,
        "retrieval_branch_failures": branch_errors,
        "fusion": {
            "method": "weighted_reciprocal_rank_fusion",
            "rrf_k": settings.rag_rrf_k,
            "weights": {
                "vector": settings.vector_weight,
                "lexical": settings.lexical_weight,
                "metadata": settings.metadata_weight,
                "graph": settings.rag_graph_weight,
            },
        },
        "total_retrieval_latency_ms": total_latency_ms,
        "metadata_boosts_used": _summarize_boosts(ranked_all[: min(10, len(ranked_all))]),
        "evidence_mix": evidence_mix,
        "top_rejected": _top_rejected_candidates(ranked_all, top_k),
    }
    return results, debug


def _should_vector_fallback(skip_vector: bool, branches: dict[str, list[dict]]) -> bool:
    return skip_vector and not any(branches[name] for name in ("lexical", "metadata", "graph"))


async def _retrieve_branches(
    db: AsyncSession,
    project_id: uuid.UUID,
    query: str,
    query_embedding: list[float],
    query_info: dict[str, Any],
    intent: QueryIntent,
    fetch_k: int,
    filters: dict[str, Any] | None,
    *,
    parallel: bool,
    timeout_seconds: float,
    branch_latencies: dict[str, int],
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Run independent candidate branches with bounded failures.

    A failed vector or lexical service must reduce recall, not turn an
    authenticated search request into a server error when another branch can
    still supply evidence.
    """
    errors: dict[str, str] = {}

    async def _run(name: str, operation) -> list[dict]:
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(operation(), timeout=max(timeout_seconds, 0.1))
        except Exception as exc:
            errors[name] = _safe_branch_error(exc)
            logger.warning("retrieval branch failed branch=%s error=%s", name, errors[name])
            return []
        finally:
            branch_latencies[f"{name}_search_ms"] = _elapsed_ms(started)

    if parallel:
        from incidentops.db.session import _get_session_factory

        factory = _get_session_factory()

        async def _vector():
            async with factory() as branch_db:
                return await vector_search(branch_db, project_id, query_embedding, top_k=fetch_k, filters=filters)

        async def _lexical():
            async with factory() as branch_db:
                return await lexical_search(branch_db, project_id, query, top_k=fetch_k, filters=filters)

        async def _metadata():
            async with factory() as branch_db:
                return await metadata_search(branch_db, project_id, query_info, top_k=fetch_k, filters=filters)

        async def _graph():
            if intent.intent != INTENT_ARCHITECTURE:
                return []
            async with factory() as branch_db:
                return await graph_search(branch_db, project_id, query_info, top_k=fetch_k)

        vector, lexical, metadata, graph = await asyncio.gather(
            _run("vector", _vector) if query_embedding else _empty_results(),
            _run("lexical", _lexical),
            _run("metadata", _metadata),
            _run("graph", _graph),
        )
        branch_latencies["retrieval_parallel"] = 1
    else:
        vector = (
            await _run("vector", lambda: vector_search(db, project_id, query_embedding, top_k=fetch_k, filters=filters))
            if query_embedding
            else []
        )
        lexical = await _run("lexical", lambda: lexical_search(db, project_id, query, top_k=fetch_k, filters=filters))
        metadata = await _run(
            "metadata", lambda: metadata_search(db, project_id, query_info, top_k=fetch_k, filters=filters)
        )
        graph = await _run(
            "graph",
            lambda: graph_search(db, project_id, query_info, top_k=fetch_k)
            if intent.intent == INTENT_ARCHITECTURE
            else _empty_results(),
        )
        branch_latencies["retrieval_parallel"] = 0
    return {"vector": vector, "lexical": lexical, "metadata": metadata, "graph": graph}, errors


def _weighted_rrf(
    *,
    vector_results: list[dict],
    lexical_results: list[dict],
    metadata_results: list[dict],
    graph_results: list[dict],
    vector_weight: float,
    lexical_weight: float,
    metadata_weight: float,
    graph_weight: float,
    rrf_k: int,
) -> dict[uuid.UUID, dict[str, Any]]:
    """Fuse independently ranked candidates without comparing score scales."""
    candidates: dict[uuid.UUID, dict[str, Any]] = {}
    branches = (
        ("vector", vector_results, vector_weight),
        ("lexical", lexical_results, lexical_weight),
        ("metadata", metadata_results, metadata_weight),
        ("graph", graph_results, graph_weight),
    )
    denominator_offset = max(rrf_k, 1)
    for branch_name, results, weight in branches:
        for rank, result in enumerate(results, start=1):
            chunk = result["chunk"]
            entry = candidates.setdefault(
                chunk.id,
                {
                    "chunk": chunk,
                    "vector_score": 0.0,
                    "lexical_score": 0.0,
                    "metadata_score": 0.0,
                    "rrf_score": 0.0,
                    "metadata_boost": 0.0,
                    "metadata_boost_reasons": [],
                    "branch_ranks": {},
                },
            )
            entry[f"{branch_name}_score"] = float(result.get("score", 0.0) or 0.0)
            entry["branch_ranks"][branch_name] = rank
            entry["rrf_score"] += (max(weight, 0.0) / (denominator_offset + rank)) * 100.0
    return candidates


def _safe_branch_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    return exc.__class__.__name__


async def _empty_results() -> list[dict]:
    return []


def _fetch_budget(top_k: int, intent: QueryIntent) -> int:
    settings = get_settings()
    multiplier = max(1, settings.rag_candidate_multiplier)
    if intent.intent in {INTENT_RUNTIME_INCIDENT, INTENT_DEPLOY_REGRESSION}:
        multiplier = max(multiplier, 4)
    if intent.intent in {INTENT_CODE_LOCATION, INTENT_CONFIG_LOOKUP, INTENT_API_CONTRACT}:
        multiplier = 3
    return max(top_k * multiplier, 30)


def _retrieval_budget_summary(intent: QueryIntent, fetch_k: int) -> dict[str, Any]:
    return {
        "fetch_k_per_branch": fetch_k,
        "preferred_source_types": list(intent.preferred_source_types),
        "preferred_chunk_types": list(intent.preferred_chunk_types),
    }


def _reason_counts(ranked: list[dict[str, Any]], *, positive: bool) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in ranked:
        for reason in item.get("metadata_boost_reasons") or []:
            is_penalty = reason.endswith("_penalty") or "penalty" in reason
            if (positive and not is_penalty) or (not positive and is_penalty):
                counts[reason] += 1
    return dict(sorted(counts.items()))


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _metadata_boost(chunk, query_info: dict[str, Any], intent: QueryIntent) -> tuple[float, list[str]]:
    boost = 0.0
    reasons: list[str] = []
    source_type = _chunk_source_type(chunk)
    chunk_type = (chunk.chunk_type or "").lower()
    metadata = chunk.metadata_json or {}
    document_path = getattr(getattr(chunk, "document", None), "path", "") or metadata.get("document_path", "") or ""
    lower_path = document_path.lower()

    if chunk.deploy_hash and chunk.deploy_hash.lower() in query_info["deploy_hashes"]:
        boost += 0.18
        reasons.append("deploy_hash_match")
    if chunk.service_name and chunk.service_name.lower() in query_info["services"]:
        boost += 0.12
        reasons.append("service_name_match")
    if chunk.endpoint:
        for endpoint in query_info["endpoints"]:
            if endpoint in chunk.endpoint or chunk.endpoint in endpoint:
                boost += 0.12
                reasons.append("endpoint_match")
                break

    if source_type in intent.preferred_source_types:
        boost += 0.16
        reasons.append(f"intent_source:{source_type}")
    if chunk_type in intent.preferred_chunk_types:
        boost += _CHUNK_TYPE_BOOSTS.get(chunk_type, 0.08)
        reasons.append(f"intent_chunk:{chunk_type}")

    query_terms = set(query_info.get("query_terms") or [])
    metadata_terms = set(_metadata_terms(metadata))
    if query_terms and metadata_terms:
        matched = sorted(query_terms & metadata_terms)[:4]
        if matched:
            boost += 0.05 * min(len(matched), 3)
            reasons.append(f"metadata_terms:{','.join(matched)}")

    if intent.intent == INTENT_CONFIG_LOOKUP:
        if any(token in lower_path for token in ("docker", "compose", "helm", "openapi", "swagger", "config", ".env", ".yaml", ".yml", ".toml", ".ini", ".json")):
            boost += 0.12
            reasons.append("config_path_match")
    if intent.intent == INTENT_API_CONTRACT:
        if any(token in lower_path for token in ("api", "openapi", "swagger", ".proto", "proto/", "routes", "router")):
            boost += 0.12
            reasons.append("api_contract_path_match")
    if intent.intent == INTENT_ARCHITECTURE:
        if any(token in lower_path for token in ("docs/", "/docs", "architecture", "design", "overview", "readme")):
            boost += 0.10
            reasons.append("architecture_doc_match")
    if intent.intent == INTENT_CODE_LOCATION:
        if any(token in lower_path for token in ("service/", "services/", "src/", "pkg/", "internal/", "cmd/", "app/")):
            boost += 0.06
            reasons.append("code_path_match")
    if intent.intent == INTENT_RUNTIME_INCIDENT and source_type == "logs":
        if metadata.get("timestamp_start") or metadata.get("log_levels") or metadata.get("trace_ids"):
            boost += 0.08
            reasons.append("runtime_log_metadata")
    if intent.intent == INTENT_DEPLOY_REGRESSION and source_type == "deploy":
        boost += 0.08
        reasons.append("deploy_regression_source")
    if intent.intent == INTENT_PREVIOUS_INCIDENT and source_type == "incident":
        if metadata.get("incident_date") or metadata.get("has_root_cause"):
            boost += 0.08
            reasons.append("incident_metadata")
    if intent.intent == INTENT_RUNBOOK_LOOKUP and source_type == "runbook":
        boost += 0.08
        reasons.append("runbook_source")

    if _should_penalize_readme(intent, lower_path):
        boost -= 0.10
        reasons.append("readme_penalty")
    if intent.intent in {INTENT_CODE_LOCATION, INTENT_API_CONTRACT} and source_type == "runbook":
        boost -= 0.24
        reasons.append("wrong_intent_runbook_penalty")
    if intent.intent == INTENT_CONFIG_LOOKUP and source_type == "runbook":
        boost -= 0.12
        reasons.append("generic_runbook_penalty")

    return boost, reasons


def _chunk_source_type(chunk) -> str:
    metadata = chunk.metadata_json or {}
    raw = metadata.get("source_type")
    if isinstance(raw, str):
        return _SOURCE_TYPE_ALIASES.get(raw, raw)
    chunk_type = (chunk.chunk_type or "").lower()
    if chunk_type in {"deploy_diff", "deploy_commit"}:
        return "deploy"
    if chunk_type in {"log_window", "error_cluster", "log_time_window", "log_error_burst"}:
        return "logs"
    if chunk_type in {
        "function",
        "class",
        "module",
        "code_file",
        "go_fallback",
        "python_fallback",
        "ts_fallback",
        "js_fallback",
        "java_fallback",
        "go_function",
        "go_method",
        "go_type",
        "go_struct",
        "go_interface",
        "go_module",
        "python_function",
        "python_class",
        "ts_function",
        "ts_class",
        "ts_interface",
        "java_method",
        "java_class",
        "java_interface",
    }:
        return "code"
    if chunk_type in {"incident_section", "incident_symptom", "incident_timeline", "incident_root_cause", "incident_action"}:
        return "incident"
    if chunk_type in {"api_endpoint", "openapi_endpoint", "openapi_schema", "proto_service", "proto_rpc", "proto_message", "proto_enum", "proto_preamble", "proto_fallback"}:
        return "api_doc"
    if chunk_type in {"config_section", "config_service_block", "env_var_block", "dependency_block", "config_fallback"}:
        return "config"
    if chunk_type in {"markdown_section", "markdown_heading_section", "markdown_procedure", "markdown_table", "markdown_faq"}:
        doc_path = getattr(getattr(chunk, "document", None), "path", "") or metadata.get("document_path", "")
        lower_path = str(doc_path).lower()
        if "api" in lower_path or "openapi" in lower_path or "swagger" in lower_path:
            return "api_doc"
        return "runbook"
    return "unknown_text"


def _metadata_terms(metadata: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for key in (
        "language",
        "service_name",
        "module_path",
        "package_path",
        "package_name",
        "title",
        "severity",
        "config_key",
        "symbol",
        "symbol_name",
        "kind",
    ):
        value = metadata.get(key)
        if isinstance(value, str):
            terms.extend(_tokenize(value))
    for key in ("symbol_names", "function_names", "class_names", "headings", "endpoint_candidates", "api_paths", "log_levels", "error_codes", "config_keys_summary", "release_markers"):
        value = metadata.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    terms.extend(_tokenize(item))
    return terms


def _tokenize(value: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9_./-]+", value.lower()) if len(token) >= 3]


def _should_penalize_readme(intent: QueryIntent, lower_path: str) -> bool:
    if intent.intent in {INTENT_ARCHITECTURE, INTENT_GENERIC}:
        return False
    if any(token in lower_path for token in README_PATH_TOKENS):
        return intent.intent in {
            INTENT_CODE_LOCATION,
            INTENT_CONFIG_LOOKUP,
            INTENT_API_CONTRACT,
            INTENT_RUNTIME_INCIDENT,
            INTENT_DEPLOY_REGRESSION,
            INTENT_PREVIOUS_INCIDENT,
            INTENT_RUNBOOK_LOOKUP,
        }
    return False


def _evidence_mix(ranked: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    source_types: dict[str, int] = {}
    chunk_types: dict[str, int] = {}
    for item in ranked:
        chunk = item["chunk"]
        source_type = _chunk_source_type(chunk)
        source_types[source_type] = source_types.get(source_type, 0) + 1
        chunk_type = chunk.chunk_type or "unknown"
        chunk_types[chunk_type] = chunk_types.get(chunk_type, 0) + 1
    return {"source_types": source_types, "chunk_types": chunk_types}


def _summarize_boosts(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for item in ranked:
        chunk = item["chunk"]
        reasons = item.get("metadata_boost_reasons") or []
        if not reasons:
            continue
        summary.append(
            {
                "document_path": getattr(getattr(chunk, "document", None), "path", "") or "",
                "chunk_id": str(chunk.id),
                "source_type": _chunk_source_type(chunk),
                "metadata_boost": round(item.get("metadata_boost", 0.0), 4),
                "reasons": reasons,
            }
        )
    return summary


def _top_rejected_candidates(ranked_all: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
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

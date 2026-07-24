"""
Reranker — cross-encoder reranking with score-fusion fallback.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from incidentops.observability.metrics import incr, observe_latency
from incidentops.retrieval.model_gateway import RemoteModelError, remote_rerank

logger = logging.getLogger("incidentops.retrieval.reranker")

_cross_encoder = None
_ce_model_name: str | None = None


def _load_cross_encoder(model_name: str):
    """Lazy-load the cross-encoder model."""
    global _cross_encoder, _ce_model_name
    if not model_name:
        return None
    from incidentops.config.settings import get_settings

    if not get_settings().allow_local_model_loading:
        logger.info("Local reranker loading is disabled; using remote or heuristic reranking")
        return None
    if _cross_encoder is None or _ce_model_name != model_name:
        try:
            from sentence_transformers import CrossEncoder
            logger.info("Loading cross-encoder: %s", model_name)
            _cross_encoder = CrossEncoder(model_name)
            _ce_model_name = model_name
        except Exception as e:
            logger.warning("Failed to load cross-encoder %s: %s. Using fallback.", model_name, e)
            _cross_encoder = None
    return _cross_encoder


def rerank(
    query: str,
    results: list[dict],
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    top_k: int = 10,
) -> list[dict]:
    reranked, _ = rerank_with_debug(query, results, model_name=model_name, top_k=top_k)
    return reranked


def rerank_with_debug(
    query: str,
    results: list[dict],
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    top_k: int = 10,
) -> tuple[list[dict], dict[str, Any]]:
    """
    Rerank results using a cross-encoder only when the top results are ambiguous.
    Falls back to fused_score ordering for decisive or exact-match lookups.
    """
    start = time.time()
    if not results:
        return [], {"mode": "skipped", "reason": "no_results", "used_model": None}

    if _should_skip_cross_encoder(results):
        _fallback_sort(results)
        latency_ms = int((time.time() - start) * 1000)
        observe_latency("rerank", latency_ms)
        return results[:top_k], {
            "mode": "heuristic",
            "reason": "decisive_retrieval_result",
            "used_model": None,
            "latency_ms": latency_ms,
        }

    encoder = _load_cross_encoder(model_name)

    if encoder is not None:
        # Cross-encoder reranking
        pairs = [(query, r["chunk"].text) for r in results]
        try:
            ce_scores = encoder.predict(pairs, show_progress_bar=False)
            for r, score in zip(results, ce_scores):
                r["rerank_score"] = float(score)
            results.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
            logger.info("Cross-encoder reranked %d results", len(results))
            mode = "cross_encoder"
            reason = "ambiguous_results"
        except Exception as e:
            incr("rerank_failures_total")
            logger.warning("Cross-encoder failed: %s. Using fused scores.", e)
            _fallback_sort(results)
            mode = "fallback"
            reason = "cross_encoder_failure"
    else:
        _fallback_sort(results)
        mode = "fallback"
        reason = "model_unavailable"

    latency_ms = int((time.time() - start) * 1000)
    observe_latency("rerank", latency_ms)
    return results[:top_k], {
        "mode": mode,
        "reason": reason,
        "used_model": model_name if mode == "cross_encoder" else None,
        "latency_ms": latency_ms,
    }


async def rerank_with_debug_async(
    query: str,
    results: list[dict],
    model_name: str = "",
    top_k: int = 10,
) -> tuple[list[dict], dict[str, Any]]:
    """Async reranking path used by cloud deployments.

    Azure ML reranking is remote and never loads a model in the Core process.
    Local deterministic/cross-encoder behavior remains available only to
    compatibility callers and contract tests.
    """
    from incidentops.config.settings import get_settings

    settings = get_settings()
    if not results:
        return [], {"mode": "skipped", "reason": "no_results", "used_model": None}
    if settings.rag_rerank_mode == "disabled":
        _fallback_sort(results)
        return results[:top_k], {"mode": "disabled", "reason": "configuration", "used_model": None}
    if model_name.startswith("azure-ml") or settings.rag_reranker_endpoint:
        if settings.rag_rerank_mode == "conditional" and _should_skip_cross_encoder(results):
            _fallback_sort(results)
            return results[:top_k], {
                "mode": "heuristic",
                "reason": "decisive_retrieval_result",
                "used_model": None,
            }
        try:
            started = time.time()
            scores = await remote_rerank(
                query,
                [
                    {"id": result["chunk"].id, "text": result["chunk"].text}
                    for result in results
                ],
            )
            for result, score in zip(results, scores):
                result["rerank_score"] = score
            results.sort(key=lambda item: item.get("rerank_score", 0.0), reverse=True)
            latency_ms = int((time.time() - started) * 1000)
            observe_latency("rerank", latency_ms)
            return results[:top_k], {
                "mode": "remote_cross_encoder",
                "reason": "ambiguous_results",
                "used_model": model_name or settings.reranker_model,
                "latency_ms": latency_ms,
            }
        except RemoteModelError:
            incr("rerank_failures_total")
            _fallback_sort(results)
            return results[:top_k], {
                "mode": "fallback",
                "reason": "remote_model_failure",
                "used_model": None,
            }
    return await _rerank_sync_off_thread(query, results, model_name, top_k)


async def _rerank_sync_off_thread(query: str, results: list[dict], model_name: str, top_k: int):
    import asyncio

    return await asyncio.to_thread(rerank_with_debug, query, results, model_name, top_k)


def _fallback_sort(results: list[dict]) -> None:
    """Sort by fused_score when cross-encoder is unavailable."""
    for r in results:
        r["rerank_score"] = r.get("fused_score", 0.0)
    results.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)


def _should_skip_cross_encoder(results: list[dict]) -> bool:
    if len(results) <= 1:
        return True
    top = results[0]
    second = results[1]
    top_score = float(top.get("fused_score", 0.0) or 0.0)
    second_score = float(second.get("fused_score", 0.0) or 0.0)
    score_gap = top_score - second_score
    top_reasons = set(top.get("metadata_boost_reasons") or [])
    if score_gap >= 0.18:
        return True
    decisive_reasons = {
        "endpoint_match",
        "service_name_match",
        "code_path_match",
        "config_path_match",
        "api_contract_path_match",
        "deploy_hash_match",
    }
    if top_reasons & decisive_reasons and score_gap >= 0.08:
        return True
    if any(reason.startswith("metadata_terms:") for reason in top_reasons) and score_gap >= 0.1:
        return True
    return False

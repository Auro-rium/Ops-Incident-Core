"""
Reranker — cross-encoder reranking with score-fusion fallback.
"""

from __future__ import annotations

import logging
import time

from incidentops.observability.metrics import incr, observe_latency

logger = logging.getLogger("incidentops.retrieval.reranker")

_cross_encoder = None
_ce_model_name: str | None = None


def _load_cross_encoder(model_name: str):
    """Lazy-load the cross-encoder model."""
    global _cross_encoder, _ce_model_name
    if not model_name:
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
    """
    Rerank results using cross-encoder if available.
    Falls back to fused_score ordering.
    """
    start = time.time()
    if not results:
        return []

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
        except Exception as e:
            incr("rerank_failures_total")
            logger.warning("Cross-encoder failed: %s. Using fused scores.", e)
            _fallback_sort(results)
    else:
        _fallback_sort(results)

    observe_latency("rerank", (time.time() - start) * 1000)
    return results[:top_k]


def _fallback_sort(results: list[dict]) -> None:
    """Sort by fused_score when cross-encoder is unavailable."""
    for r in results:
        r["rerank_score"] = r.get("fused_score", 0.0)
    results.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)

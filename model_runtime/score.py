"""Pinned GPU model scoring used by the managed cloud reranker endpoint.

Core calls this service through a provider-specific managed endpoint. Core does
not download or load these model classes.
"""

from __future__ import annotations

import os
import time

import torch


MODEL_ROLE = os.environ.get("MODEL_ROLE", "embedding")
EMBEDDING_MODEL_ID = os.environ.get("EMBEDDING_MODEL_ID", "BAAI/bge-m3")
RERANKER_MODEL_ID = os.environ.get("RERANKER_MODEL_ID", "BAAI/bge-reranker-v2-m3")
MODEL_REVISION = os.environ.get("MODEL_REVISION", "")
_model = None


def init() -> None:
    global _model
    if not torch.cuda.is_available():
        raise RuntimeError("GPU is required for the IncidentOps model runtime")
    if not MODEL_REVISION:
        raise RuntimeError("MODEL_REVISION must pin an immutable Hugging Face revision")
    if MODEL_ROLE == "embedding":
        from FlagEmbedding import BGEM3FlagModel

        _model = BGEM3FlagModel(EMBEDDING_MODEL_ID, use_fp16=True, revision=MODEL_REVISION)
    elif MODEL_ROLE == "reranker":
        from FlagEmbedding import FlagReranker

        _model = FlagReranker(RERANKER_MODEL_ID, use_fp16=True, revision=MODEL_REVISION)
    else:
        raise RuntimeError("MODEL_ROLE must be embedding or reranker")


def run(payload: dict) -> dict:
    if _model is None:
        raise RuntimeError("model was not initialized")
    started = time.perf_counter()
    if MODEL_ROLE == "embedding":
        inputs = payload.get("inputs") or []
        texts = [str(item["text"]) for item in inputs]
        encoded = _model.encode(texts, return_dense=True, return_sparse=False, return_colbert_vecs=False)
        vectors = encoded["dense_vecs"].tolist()
        return {
            "model_id": EMBEDDING_MODEL_ID,
            "model_revision": MODEL_REVISION,
            "dimension": len(vectors[0]) if vectors else 1024,
            "vectors": vectors,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "batch_size": len(vectors),
        }
    query = str(payload["query"])
    candidates = payload.get("candidates") or []
    scores = _model.compute_score([(query, str(candidate["text"])) for candidate in candidates], normalize=True)
    if not isinstance(scores, list):
        scores = [scores]
    return {
        "model_id": RERANKER_MODEL_ID,
        "model_revision": MODEL_REVISION,
        "scores": [float(score) for score in scores],
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }

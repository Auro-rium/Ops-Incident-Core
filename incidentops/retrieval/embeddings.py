"""
Embedding helpers.

Default behavior is offline-safe via a deterministic hash embedding backend.
Sentence-transformers remains supported if explicitly configured.
"""

from __future__ import annotations

import hashlib
import logging
import math

import httpx

from incidentops.config.settings import get_settings

logger = logging.getLogger("incidentops.retrieval.embeddings")

_model = None
_model_name: str | None = None


def _load_model(model_name: str):
    global _model, _model_name
    if model_name.startswith("local-hash"):
        return None
    if _model is None or _model_name != model_name:
        logger.info("Loading embedding model: %s", model_name)
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(model_name)
        _model_name = model_name
        logger.info("Model loaded: %s (dim=%d)", model_name, _model.get_sentence_embedding_dimension())
    return _model


def _hash_embed(text: str, dim: int) -> list[float]:
    values = [0.0] * dim
    for token in text.lower().split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        values[index] += sign
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def _azure_embed(texts: list[str]) -> list[list[float]]:
    settings = get_settings()
    if not settings.azure_openai_embeddings_configured:
        raise RuntimeError("Azure OpenAI embedding deployment is not configured")
    url = (
        f"{settings.azure_openai_endpoint.rstrip('/')}/openai/deployments/"
        f"{settings.azure_openai_embedding_deployment}/embeddings"
    )
    payload = {
        "input": texts,
        "dimensions": settings.embedding_dim,
    }
    headers = {
        "api-key": settings.azure_openai_api_key,
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=float(settings.llm_timeout_seconds)) as client:
        response = client.post(
            url,
            params={"api-version": settings.azure_openai_api_version},
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
    data = response.json()["data"]
    ordered = sorted(data, key=lambda item: item.get("index", 0))
    embeddings = [item["embedding"] for item in ordered]
    for embedding in embeddings:
        norm = math.sqrt(sum(float(value) * float(value) for value in embedding)) or 1.0
        for index, value in enumerate(embedding):
            embedding[index] = float(value) / norm
    return embeddings


def embed_texts(texts: list[str], model_name: str | None = None) -> list[list[float]]:
    if not texts:
        return []
    settings = get_settings()
    chosen_model = model_name or settings.embedding_model
    if chosen_model.startswith("local-hash"):
        return [_hash_embed(text, settings.embedding_dim) for text in texts]
    if chosen_model.startswith("azure-openai"):
        return _azure_embed(texts)
    try:
        model = _load_model(chosen_model)
        embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        return [embedding.tolist() for embedding in embeddings]
    except Exception as exc:
        if settings.is_production_like:
            raise
        logger.warning("Falling back to local hash embeddings after model load failure: %s", exc)
        return [_hash_embed(text, settings.embedding_dim) for text in texts]


def embed_query(query: str, model_name: str | None = None) -> list[float]:
    result = embed_texts([query], model_name)
    return result[0] if result else []


def get_embedding_dimension(model_name: str | None = None) -> int:
    settings = get_settings()
    chosen_model = model_name or settings.embedding_model
    if chosen_model.startswith("local-hash") or chosen_model.startswith("azure-openai"):
        return settings.embedding_dim
    model = _load_model(chosen_model)
    return model.get_sentence_embedding_dimension()

"""
Embedding helpers.

Default behavior is offline-safe via a deterministic hash embedding backend.
Sentence-transformers remains supported if explicitly configured.
"""

from __future__ import annotations

import hashlib
import asyncio
import logging
import math
import random
import time

import httpx

from incidentops.config.settings import get_settings
from incidentops.retrieval.cache import get_embedding, set_embedding
from incidentops.retrieval.model_gateway import remote_embed

logger = logging.getLogger("incidentops.retrieval.embeddings")

_model = None
_model_name: str | None = None
_last_azure_embed_request_at = 0.0

_RETRYABLE_EMBEDDING_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


def _load_model(model_name: str):
    global _model, _model_name
    if model_name.startswith("local-hash"):
        return None
    if not get_settings().allow_local_model_loading:
        raise RuntimeError("local model loading is disabled; use Azure ML or Azure OpenAI")
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
        for attempt in range(settings.embedding_request_max_retries + 1):
            _respect_embedding_min_interval(settings)
            try:
                response = client.post(
                    url,
                    params={"api-version": settings.azure_openai_api_version},
                    headers=headers,
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= settings.embedding_request_max_retries:
                    raise
                delay = _embedding_retry_delay(None, attempt, settings)
                logger.warning(
                    "Azure embedding request failed with %s; retrying in %.2fs (attempt %d/%d)",
                    exc.__class__.__name__,
                    delay,
                    attempt + 1,
                    settings.embedding_request_max_retries,
                )
                time.sleep(delay)
                continue
            if response.status_code in _RETRYABLE_EMBEDDING_STATUS_CODES and attempt < settings.embedding_request_max_retries:
                delay = _embedding_retry_delay(response, attempt, settings)
                logger.warning(
                    "Azure embedding request returned HTTP %d; retrying in %.2fs (attempt %d/%d)",
                    response.status_code,
                    delay,
                    attempt + 1,
                    settings.embedding_request_max_retries,
                )
                time.sleep(delay)
                continue
            response.raise_for_status()
            break
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
    if chosen_model.startswith("azure-ml"):
        raise RuntimeError("azure-ml embeddings require the asynchronous remote gateway")
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


async def embed_texts_async(texts: list[str], model_name: str | None = None) -> list[list[float]]:
    if not texts:
        return []
    settings = get_settings()
    chosen_model = model_name or settings.embedding_model
    if chosen_model.startswith("azure-ml"):
        cached: list[list[float] | None] = await asyncio.gather(
            *(get_embedding(text) for text in texts)
        )
        missing_indexes = [index for index, vector in enumerate(cached) if vector is None]
        if missing_indexes:
            generated = await remote_embed([texts[index] for index in missing_indexes])
            await asyncio.gather(
                *(set_embedding(texts[index], vector) for index, vector in zip(missing_indexes, generated))
            )
            for index, vector in zip(missing_indexes, generated):
                cached[index] = vector
        return [vector for vector in cached if vector is not None]
    return await asyncio.to_thread(embed_texts, texts, chosen_model)


async def embed_query_async(query: str, model_name: str | None = None) -> list[float]:
    vectors = await embed_texts_async([query], model_name=model_name)
    return vectors[0] if vectors else []


def _respect_embedding_min_interval(settings) -> None:
    global _last_azure_embed_request_at
    min_interval = max(0.0, float(settings.embedding_request_min_interval_seconds))
    if min_interval <= 0:
        _last_azure_embed_request_at = time.monotonic()
        return
    elapsed = time.monotonic() - _last_azure_embed_request_at
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_azure_embed_request_at = time.monotonic()


def _embedding_retry_delay(response: httpx.Response | None, attempt: int, settings) -> float:
    if response is not None:
        retry_after_ms = response.headers.get("x-ms-retry-after-ms")
        if retry_after_ms:
            try:
                return max(0.0, min(float(retry_after_ms) / 1000.0, float(settings.embedding_request_max_backoff_seconds)))
            except ValueError:
                pass
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), float(settings.embedding_request_max_backoff_seconds)))
            except ValueError:
                pass
    base = float(settings.embedding_request_initial_backoff_seconds) * (2**attempt)
    capped = min(base, float(settings.embedding_request_max_backoff_seconds))
    jitter = min(0.5, capped * 0.1)
    return capped + random.uniform(0.0, jitter)

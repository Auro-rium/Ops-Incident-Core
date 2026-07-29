from __future__ import annotations

import asyncio
import json
import time

from incidentops.config.settings import get_settings
from incidentops.config.validation import validate_startup_settings
from incidentops.llm.provider import LLMProvider
from incidentops.retrieval.embeddings import embed_texts
from incidentops.retrieval.model_gateway import remote_rerank


async def main() -> None:
    settings = get_settings()
    validate_startup_settings(settings)

    embedding_started = time.perf_counter()
    vectors = embed_texts(["IncidentOps AWS model preflight"])
    embedding_latency_ms = round((time.perf_counter() - embedding_started) * 1000, 2)
    if len(vectors) != 1 or len(vectors[0]) != settings.embedding_dim:
        raise RuntimeError("embedding preflight returned an invalid dimension")

    chat_started = time.perf_counter()
    response = await LLMProvider().chat(
        [
            {"role": "system", "content": "Return only a small JSON object."},
            {"role": "user", "content": 'Return {"ok": true}.'},
        ],
        max_tokens=32,
    )
    chat_latency_ms = round((time.perf_counter() - chat_started) * 1000, 2)
    if response is None:
        raise RuntimeError("Bedrock chat preflight returned no response")

    reranker_latency_ms = None
    if settings.sagemaker_reranker_configured:
        rerank_started = time.perf_counter()
        scores = await remote_rerank(
            "Where is request routing implemented?",
            [
                {"id": "1", "text": "The router dispatches incoming API requests."},
                {"id": "2", "text": "The release notes describe dependency updates."},
            ],
        )
        reranker_latency_ms = round((time.perf_counter() - rerank_started) * 1000, 2)
        if len(scores) != 2:
            raise RuntimeError("SageMaker reranker preflight returned an invalid score count")

    usage = response.get("_meta", {}).get("usage", {}) if isinstance(response, dict) else {}
    print(
        json.dumps(
            {
                "provider": "aws",
                "chat_model": settings.bedrock_chat_model_id,
                "embedding_model": settings.bedrock_embedding_model_id,
                "embedding_dimension": len(vectors[0]),
                "embedding_latency_ms": embedding_latency_ms,
                "chat_latency_ms": chat_latency_ms,
                "input_tokens": int(usage.get("inputTokens", 0) or 0),
                "output_tokens": int(usage.get("outputTokens", 0) or 0),
                "reranker_configured": settings.sagemaker_reranker_configured,
                "reranker_latency_ms": reranker_latency_ms,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

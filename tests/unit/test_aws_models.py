from __future__ import annotations

import json
from io import BytesIO

import pytest

from apps.api.routes.runtime import build_runtime_status
from incidentops.config.settings import Settings
from incidentops.config.validation import production_settings_errors
from incidentops.llm import provider as llm_provider
from incidentops.retrieval import embeddings, model_gateway


def aws_production_settings(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "cloud_provider": "aws",
        "require_aws_models": True,
        "require_azure_openai": False,
        "embedding_model": "aws-bedrock-titan-v2",
        "embedding_dim": 1024,
        "aws_region": "us-east-1",
        "bedrock_chat_model_id": "anthropic.claude-3-5-sonnet-20240620-v1:0",
        "bedrock_embedding_model_id": "amazon.titan-embed-text-v2:0",
        "sagemaker_reranker_endpoint_name": "incidentops-reranker",
        "reranker_model": "BAAI/bge-reranker-v2-m3",
        "rag_gpu_endpoint_required": True,
        "rag_async_indexing": True,
        "rag_model_revision": "pinned-revision",
        "jwt_secret": "x" * 40,
        "allow_demo_project_bypass": False,
        "allow_local_seed_admin": False,
        "rate_limit_backend": "redis",
        "worker_mode": "queue",
        "job_queue_backend": "redis",
        "metrics_backend": "prometheus",
        "local_ingest_enabled": False,
        "cors_allow_origins": "https://incidentops.example.com",
        "allow_wildcard_cors": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_aws_production_settings_are_accepted_and_provider_specific() -> None:
    settings = aws_production_settings()

    assert production_settings_errors(settings) == []
    assert settings.effective_cloud_provider == "aws"
    assert settings.cloud_embeddings_configured is True
    assert settings.gpu_rag_configured is True


def test_aws_production_settings_reject_missing_models() -> None:
    errors = production_settings_errors(
        aws_production_settings(
            bedrock_chat_model_id="",
            bedrock_embedding_model_id="",
            sagemaker_reranker_endpoint_name="",
        )
    )

    assert any("BEDROCK_CHAT_MODEL_ID" in error for error in errors)
    assert any("BEDROCK_EMBEDDING_MODEL_ID" in error for error in errors)
    assert any("SAGEMAKER_RERANKER_ENDPOINT_NAME" in error for error in errors)


def test_runtime_status_reports_aws_without_credentials() -> None:
    status = build_runtime_status(aws_production_settings())
    payload = status.model_dump()

    assert payload["cloud_provider"] == "aws"
    assert payload["llm_provider"] == "amazon_bedrock"
    assert payload["embedding_backend"] == "aws_bedrock"
    assert payload["bedrock_configured"] is True
    assert payload["sagemaker_reranker_configured"] is True
    assert payload["local_fallback_active"] is False
    assert "secret" not in repr(payload).lower()


@pytest.mark.asyncio
async def test_bedrock_chat_maps_messages_usage_and_json(monkeypatch) -> None:
    settings = aws_production_settings()
    monkeypatch.setattr(llm_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(
        llm_provider,
        "_invoke_bedrock_converse",
        lambda **_kwargs: {
            "output": {"message": {"content": [{"text": '{"answer":"bounded"}'}]}},
            "usage": {"inputTokens": 11, "outputTokens": 3},
        },
    )

    result = await llm_provider.LLMProvider().chat(
        [{"role": "system", "content": "Return JSON"}, {"role": "user", "content": "question"}]
    )

    assert result is not None
    assert result["answer"] == "bounded"
    assert result["_meta"]["provider"] == "amazon_bedrock"
    assert result["_meta"]["usage"]["total_tokens"] == 14


def test_bedrock_embeddings_preserve_order_and_dimension(monkeypatch) -> None:
    settings = aws_production_settings(embedding_dim=4, bedrock_embedding_max_concurrency=2)

    class FakeClient:
        def invoke_model(self, **kwargs):
            text = json.loads(kwargs["body"])["inputText"]
            vector = [1.0, 0.0, 0.0, 0.0] if text == "first" else [0.0, 1.0, 0.0, 0.0]
            return {"body": BytesIO(json.dumps({"embedding": vector}).encode("utf-8"))}

    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: FakeClient())

    assert embeddings._bedrock_embed(["first", "second"]) == [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
    ]


@pytest.mark.asyncio
async def test_sagemaker_reranker_uses_bounded_contract(monkeypatch) -> None:
    settings = aws_production_settings(rag_rerank_max_chars_per_candidate=8)
    captured = {}

    def fake_invoke(**kwargs):
        captured.update(kwargs)
        return {"scores": [0.9, 0.2]}

    monkeypatch.setattr(model_gateway, "_invoke_sagemaker_endpoint", fake_invoke)
    scores = await model_gateway._sagemaker_rerank(
        "history latency",
        [
            {"id": "one", "text": "first candidate"},
            {"id": "two", "text": "second candidate"},
        ],
        settings,
    )

    assert scores == [0.9, 0.2]
    assert captured["endpoint_name"] == "incidentops-reranker"
    assert all(len(item["text"]) <= 8 for item in captured["payload"]["candidates"])

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from incidentops.llm import provider as provider_module
from incidentops.retrieval import cache, embeddings


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


class _FakeResponse:
    def __init__(self, status_code: int, payload: object, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = ""
        self.request = httpx.Request("POST", "https://example.invalid")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("request failed", request=self.request, response=self)

    def json(self) -> object:
        return self._payload


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, *args, **kwargs):
        response = self._responses[self.calls]
        self.calls += 1
        return response


def _retry_settings(**overrides):
    values = dict(
        llm_timeout_seconds=5,
        embedding_request_max_retries=2,
        embedding_request_initial_backoff_seconds=0.01,
        embedding_request_max_backoff_seconds=0.05,
        embedding_request_min_interval_seconds=0.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_embedding_cache_key_includes_model_and_input_type(monkeypatch):
    redis = _FakeRedis()

    async def fake_client():
        return redis

    monkeypatch.setattr(cache, "_redis_client", fake_client)

    async def scenario():
        await cache.set_embedding("checkout latency", [1.0, 0.0], model="model-a", input_type="passage")
        return (
            await cache.get_embedding("checkout latency", model="model-a", input_type="passage"),
            await cache.get_embedding("checkout latency", model="model-b", input_type="passage"),
            await cache.get_embedding("checkout latency", model="model-a", input_type="query"),
        )

    same, other_model, other_input = asyncio.run(scenario())

    assert same == [1.0, 0.0]
    assert other_model is None
    assert other_input is None


def test_query_embedding_does_not_reuse_cached_passage_vector(monkeypatch):
    redis = _FakeRedis()
    vectors = iter([[[1.0, 0.0]], [[0.0, 1.0]]])

    async def fake_client():
        return redis

    async def fake_remote_embed(texts):
        return next(vectors)

    monkeypatch.setattr(cache, "_redis_client", fake_client)
    monkeypatch.setattr(embeddings, "remote_embed", fake_remote_embed)

    async def scenario():
        await embeddings.embed_texts_async(["checkout"], model_name="azure-ml-embed", input_type="passage")
        return await embeddings.embed_query_async("checkout", "azure-ml-embed")

    assert asyncio.run(scenario()) == [0.0, 1.0]


def test_nvidia_embed_retries_throttled_requests(monkeypatch):
    settings = _retry_settings(
        nvidia_embeddings_configured=True,
        nvidia_api_key="token",
        nvidia_embedding_model="nvidia/nemotron",
        nvidia_embedding_endpoint="https://integrate.api.nvidia.com/v1/embeddings",
        embedding_dim=2,
    )
    fake_client = _FakeClient(
        [
            _FakeResponse(429, {"error": "throttled"}, headers={"retry-after": "0"}),
            _FakeResponse(200, {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}),
        ]
    )
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embeddings.httpx, "Client", lambda timeout: fake_client)
    monkeypatch.setattr(embeddings.time, "sleep", lambda seconds: None)

    result = embeddings.embed_texts(["checkout"], model_name="nvidia/nemotron", input_type="query")

    assert fake_client.calls == 2
    assert result == [[1.0, 0.0]]


def test_azure_embed_rejects_wrong_dimension(monkeypatch):
    settings = _retry_settings(
        azure_openai_embeddings_configured=True,
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_embedding_deployment="embed",
        azure_openai_api_key="secret",
        azure_openai_api_version="2024-10-21",
        embedding_dim=4,
    )
    fake_client = _FakeClient([_FakeResponse(200, {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})])
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embeddings.httpx, "Client", lambda timeout: fake_client)

    with pytest.raises(RuntimeError, match="dimension"):
        embeddings.embed_texts(["checkout"], model_name="azure-openai")


def test_huggingface_embed_rejects_wrong_dimension(monkeypatch):
    settings = _retry_settings(
        hf_embeddings_configured=True,
        hf_api_token="token",
        hf_embedding_model="thenlper/gte-large",
        hf_embedding_endpoint="https://router.huggingface.co/hf-inference",
        embedding_dim=4,
    )
    fake_client = _FakeClient([_FakeResponse(200, [[1.0, 0.0]])])
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embeddings.httpx, "Client", lambda timeout: fake_client)

    with pytest.raises(RuntimeError, match="dimension"):
        embeddings.embed_texts(["checkout"], model_name="huggingface")


def test_sentence_transformer_rejects_wrong_dimension_without_hash_fallback(monkeypatch):
    settings = SimpleNamespace(embedding_dim=4, embedding_model="all-MiniLM", is_production_like=False)

    class _Vector(list):
        def tolist(self):
            return list(self)

    class _Model:
        def encode(self, texts, **kwargs):
            return [_Vector([1.0, 0.0]) for _ in texts]

    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embeddings, "_load_model", lambda name: _Model())

    with pytest.raises(RuntimeError, match="dimension"):
        embeddings.embed_texts(["checkout"], model_name="all-MiniLM")


def test_azure_chat_respects_caller_max_tokens(monkeypatch):
    captured: dict = {}

    class _AsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, headers, json):
            captured["payload"] = dict(json)
            return _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})

    llm = object.__new__(provider_module.LLMProvider)
    llm.azure_mode = True
    llm.base_url = "https://example.openai.azure.com"
    llm.api_key = "secret"
    llm.model = "chat"
    llm.api_version = "2024-10-21"
    llm.provider_name = "azure_openai"
    llm.timeout = 5
    llm.available = True
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", _AsyncClient)

    asyncio.run(llm.chat([{"role": "user", "content": "hi"}], max_tokens=1024))

    assert captured["payload"]["max_completion_tokens"] == 1024

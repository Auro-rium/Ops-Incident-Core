from __future__ import annotations

from types import SimpleNamespace

import httpx

from incidentops.retrieval import embeddings


class _FakeResponse:
    def __init__(self, status_code: int, payload: object, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
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


def test_azure_embed_retries_throttled_requests(monkeypatch):
    settings = SimpleNamespace(
        azure_openai_embeddings_configured=True,
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_embedding_deployment="incidentops-embed",
        azure_openai_api_key="secret",
        azure_openai_api_version="2024-10-21",
        embedding_dim=4,
        llm_timeout_seconds=5,
        embedding_request_max_retries=3,
        embedding_request_initial_backoff_seconds=0.01,
        embedding_request_max_backoff_seconds=0.05,
        embedding_request_min_interval_seconds=0.0,
    )
    fake_client = _FakeClient(
        [
            _FakeResponse(429, {"error": {"message": "throttled"}}, headers={"retry-after": "0"}),
            _FakeResponse(200, {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}]}),
        ]
    )
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embeddings.httpx, "Client", lambda timeout: fake_client)
    monkeypatch.setattr(embeddings.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(embeddings.random, "uniform", lambda a, b: 0.0)

    result = embeddings.embed_texts(["history service"], model_name="azure-openai")

    assert len(result) == 1
    assert fake_client.calls == 2
    assert result[0] == [1.0, 0.0, 0.0, 0.0]


def test_huggingface_embed_accepts_feature_extraction_batch(monkeypatch):
    settings = SimpleNamespace(
        hf_embeddings_configured=True,
        hf_api_token="token",
        hf_embedding_model="thenlper/gte-large",
        hf_embedding_endpoint="https://router.huggingface.co/hf-inference",
        llm_timeout_seconds=5,
        embedding_request_max_retries=0,
        embedding_request_initial_backoff_seconds=0.01,
        embedding_request_max_backoff_seconds=0.05,
        embedding_request_min_interval_seconds=0.0,
    )
    fake_client = _FakeClient([_FakeResponse(200, [[1.0, 0.0], [0.0, 1.0]])])
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embeddings.httpx, "Client", lambda timeout: fake_client)

    result = embeddings.embed_texts(["history service", "workflow task"], model_name="huggingface")

    assert fake_client.calls == 1
    assert result == [[1.0, 0.0], [0.0, 1.0]]


def test_huggingface_embed_pools_token_level_feature_extraction(monkeypatch):
    settings = SimpleNamespace(
        hf_embeddings_configured=True,
        hf_api_token="token",
        hf_embedding_model="thenlper/gte-large",
        hf_embedding_endpoint="https://router.huggingface.co/hf-inference",
        llm_timeout_seconds=5,
        embedding_request_max_retries=0,
        embedding_request_initial_backoff_seconds=0.01,
        embedding_request_max_backoff_seconds=0.05,
        embedding_request_min_interval_seconds=0.0,
    )
    fake_client = _FakeClient(
        [_FakeResponse(200, [[[1.0, 0.0], [0.0, 1.0]], [[2.0, 2.0]]])]
    )
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embeddings.httpx, "Client", lambda timeout: fake_client)

    result = embeddings.embed_texts(["history service", "workflow task"], model_name="huggingface")

    assert result == [[0.5, 0.5], [2.0, 2.0]]


def test_nvidia_nemotron_embed_preserves_provider_order_and_input_type(monkeypatch):
    settings = SimpleNamespace(
        nvidia_embeddings_configured=True,
        nvidia_api_key="token",
        nvidia_embedding_model="nvidia/nemotron-3-embed-1b",
        nvidia_embedding_endpoint="https://integrate.api.nvidia.com/v1/embeddings",
        embedding_dim=2,
        llm_timeout_seconds=5,
    )
    fake_client = _FakeClient(
        [
            _FakeResponse(
                200,
                {
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                },
            )
        ]
    )
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embeddings.httpx, "Client", lambda timeout: fake_client)

    result = embeddings.embed_texts(
        ["history service", "workflow task"],
        model_name="nvidia/nemotron-3-embed-1b",
        input_type="query",
    )

    assert fake_client.calls == 1
    assert result == [[1.0, 0.0], [0.0, 1.0]]

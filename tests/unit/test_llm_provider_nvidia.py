from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from incidentops.config.settings import Settings
from incidentops.llm import provider as provider_module

ENDPOINT = "https://nvidia.example.invalid/v1"
MODEL = "nvidia/test-chat"


@pytest.fixture
def llm(monkeypatch):
    settings = Settings(
        _env_file=None,
        cloud_provider="nvidia",
        nvidia_api_key="nv-secret",
        nvidia_chat_endpoint=f"{ENDPOINT}/",
        nvidia_chat_model=MODEL,
    )
    monkeypatch.setattr(provider_module, "get_settings", lambda: settings)
    return provider_module.LLMProvider()


def _mock(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        provider_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )


def _completion(content, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": usage or {}})


def _chat(llm, **kwargs):
    return asyncio.run(llm.chat([{"role": "user", "content": "hi"}], **kwargs))


def test_nvidia_provider_is_selected_from_settings(llm):
    assert llm.provider_name == "nvidia"
    assert llm.available is True
    assert llm.azure_mode is False


def test_nvidia_chat_sends_expected_request_and_returns_metadata(llm, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content)
        return _completion('{"summary": "ok"}', {"prompt_tokens": 7, "completion_tokens": 3})

    _mock(monkeypatch, handler)

    result = _chat(llm, max_tokens=321, response_format={"type": "json_object"})

    assert captured["url"] == f"{ENDPOINT}/chat/completions"
    assert captured["authorization"] == "Bearer nv-secret"
    assert captured["payload"]["model"] == MODEL
    assert captured["payload"]["max_tokens"] == 321
    assert "max_completion_tokens" not in captured["payload"]
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert result["summary"] == "ok"
    assert result["_meta"]["provider"] == "nvidia"
    assert result["_meta"]["model"] == MODEL
    assert result["_meta"]["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}


@pytest.mark.parametrize("content", ["plain prose", "[1, 2]", '"text"', "42", "null"])
def test_nvidia_chat_wraps_non_object_replies_as_raw_response(llm, monkeypatch, content):
    _mock(monkeypatch, lambda request: _completion(content))

    result = _chat(llm)

    assert result["raw_response"] == content
    assert result["_meta"]["provider"] == "nvidia"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, {"error": "upstream failure"}),
        (429, {"error": "slow down"}),
        (200, {"choices": []}),
        (200, {"choices": [{"message": {"content": None}}]}),
    ],
)
def test_nvidia_chat_failures_return_none(llm, monkeypatch, status, body):
    _mock(monkeypatch, lambda request: httpx.Response(status, json=body))

    assert _chat(llm) is None


def test_nvidia_chat_transport_error_returns_none(llm, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _mock(monkeypatch, handler)

    assert _chat(llm) is None


def test_unconfigured_nvidia_key_never_calls_the_network(monkeypatch):
    settings = Settings(_env_file=None, cloud_provider="nvidia", nvidia_api_key="", llm_api_key="")
    monkeypatch.setattr(provider_module, "get_settings", lambda: settings)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    _mock(monkeypatch, handler)

    assert _chat(provider_module.LLMProvider()) is None

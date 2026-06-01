from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from incidentops.config.settings import get_settings
from incidentops.observability.metrics import incr, observe_latency

logger = logging.getLogger("incidentops.llm.provider")


class LLMProvider:
    def __init__(self):
        settings = get_settings()
        azure_key = settings.azure_openai_api_key.strip()
        self.azure_mode = bool(
            settings.azure_openai_endpoint
            and azure_key
            and azure_key != "disabled"
            and settings.azure_openai_chat_deployment
        )
        if self.azure_mode:
            self.base_url = settings.azure_openai_endpoint.rstrip("/")
            self.api_key = settings.azure_openai_api_key
            self.model = settings.azure_openai_chat_deployment
            self.api_version = settings.azure_openai_api_version
        elif not settings.is_production_like:
            self.base_url = settings.llm_base_url.rstrip("/")
            self.api_key = settings.llm_api_key
            self.model = settings.llm_model
            self.api_version = ""
        else:
            self.base_url = ""
            self.api_key = ""
            self.model = ""
            self.api_version = ""
        self.timeout = settings.llm_timeout_seconds
        self.available = bool(self.api_key)

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 2048,
        response_format: dict | None = None,
    ) -> dict | None:
        if not self.available:
            logger.warning("LLM not configured — skipping generation")
            return None
        headers = {"Content-Type": "application/json"}
        if self.azure_mode:
            headers["api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
        }
        token_limit_key = "max_completion_tokens" if self.azure_mode else "max_tokens"
        payload[token_limit_key] = max(max_tokens, 4096) if self.azure_mode else max_tokens
        if not self.azure_mode:
            payload["model"] = self.model
        if response_format:
            payload["response_format"] = response_format
        try:
            async with httpx.AsyncClient(timeout=float(self.timeout)) as client:
                import time

                start = time.time()
                if self.azure_mode:
                    url = (
                        f"{self.base_url}/openai/deployments/{self.model}/chat/completions"
                        f"?api-version={self.api_version}"
                    )
                else:
                    url = f"{self.base_url}/chat/completions"
                resp = await client.post(url, headers=headers, json=payload)
                if (
                    self.azure_mode
                    and resp.status_code == 400
                    and token_limit_key == "max_completion_tokens"
                    and "max_completion_tokens" in resp.text
                    and "Unsupported parameter" in resp.text
                ):
                    payload["max_tokens"] = payload.pop("max_completion_tokens")
                    token_limit_key = "max_tokens"
                    resp = await client.post(url, headers=headers, json=payload)
                if (
                    self.azure_mode
                    and resp.status_code == 400
                    and "temperature" in resp.text
                    and "Unsupported value" in resp.text
                ):
                    payload.pop("temperature", None)
                    resp = await client.post(url, headers=headers, json=payload)
                latency_ms = int((time.time() - start) * 1000)
                observe_latency("llm", latency_ms)
                observe_latency("llm_latency", latency_ms)
                resp.raise_for_status()
                data = resp.json()
                incr("llm_calls")
                incr("llm_calls_total")
                meta = {
                    "provider": "azure_openai" if self.azure_mode else "openai_compatible",
                    "model": self.model,
                    "latency_ms": latency_ms,
                    "usage": data.get("usage") or {},
                }
                if usage := data.get("usage"):
                    incr("llm_prompt_tokens", usage.get("prompt_tokens", 0))
                    incr("llm_completion_tokens", usage.get("completion_tokens", 0))
                    incr("llm_input_tokens_total", usage.get("prompt_tokens", 0))
                    incr("llm_output_tokens_total", usage.get("completion_tokens", 0))
                content = data["choices"][0]["message"]["content"]
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        parsed["_meta"] = meta
                    return parsed
                except json.JSONDecodeError:
                    return {"raw_response": content, "_meta": meta}
        except httpx.HTTPStatusError as exc:
            logger.error("LLM HTTP error %d: %s", exc.response.status_code, exc.response.text[:200])
            incr("llm_failures")
            incr("llm_failures_total")
            return None
        except Exception as exc:
            logger.error("LLM request failed: %s", exc)
            incr("llm_failures")
            incr("llm_failures_total")
            return None


_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        _provider = LLMProvider()
    return _provider

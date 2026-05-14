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
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model
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
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        try:
            async with httpx.AsyncClient(timeout=float(self.timeout)) as client:
                import time

                start = time.time()
                resp = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
                latency_ms = int((time.time() - start) * 1000)
                observe_latency("llm", latency_ms)
                resp.raise_for_status()
                data = resp.json()
                incr("llm_calls")
                if usage := data.get("usage"):
                    incr("llm_prompt_tokens", usage.get("prompt_tokens", 0))
                    incr("llm_completion_tokens", usage.get("completion_tokens", 0))
                content = data["choices"][0]["message"]["content"]
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    return {"raw_response": content}
        except httpx.HTTPStatusError as exc:
            logger.error("LLM HTTP error %d: %s", exc.response.status_code, exc.response.text[:200])
            incr("llm_failures")
            return None
        except Exception as exc:
            logger.error("LLM request failed: %s", exc)
            incr("llm_failures")
            return None


_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        _provider = LLMProvider()
    return _provider

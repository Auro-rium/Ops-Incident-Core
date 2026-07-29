from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from incidentops.config.settings import get_settings
from incidentops.observability.metrics import incr, observe_latency

logger = logging.getLogger("incidentops.llm.provider")


class LLMProvider:
    def __init__(self):
        settings = get_settings()
        self.aws_mode = settings.effective_cloud_provider == "aws" and settings.bedrock_chat_configured
        azure_key = settings.azure_openai_api_key.strip()
        self.azure_mode = not self.aws_mode and bool(
            settings.azure_openai_endpoint
            and azure_key
            and azure_key != "disabled"
            and settings.azure_openai_chat_deployment
        )
        self.aws_region = settings.aws_region
        if self.aws_mode:
            self.base_url = ""
            self.api_key = ""
            self.model = settings.bedrock_chat_model_id
            self.api_version = ""
        elif self.azure_mode:
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
        self.available = self.aws_mode or bool(self.api_key)

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
        if self.aws_mode:
            return await self._bedrock_chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
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
            logger.error("LLM request failed: %s", exc.__class__.__name__)
            incr("llm_failures")
            incr("llm_failures_total")
            return None

    async def _bedrock_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> dict | None:
        started = time.perf_counter()
        try:
            response = await asyncio.to_thread(
                _invoke_bedrock_converse,
                region=self.aws_region,
                model_id=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_seconds=self.timeout,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            observe_latency("llm", latency_ms)
            observe_latency("llm_latency", latency_ms)
            incr("llm_calls")
            incr("llm_calls_total")
            usage = response.get("usage") or {}
            input_tokens = int(usage.get("inputTokens", 0) or 0)
            output_tokens = int(usage.get("outputTokens", 0) or 0)
            incr("llm_prompt_tokens", input_tokens)
            incr("llm_completion_tokens", output_tokens)
            incr("llm_input_tokens_total", input_tokens)
            incr("llm_output_tokens_total", output_tokens)
            content_blocks = ((response.get("output") or {}).get("message") or {}).get("content") or []
            content = "".join(
                str(block.get("text", "")) for block in content_blocks if isinstance(block, dict)
            ).strip()
            if not content:
                raise ValueError("Bedrock response did not contain text")
            meta = {
                "provider": "amazon_bedrock",
                "model": self.model,
                "latency_ms": latency_ms,
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            }
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    parsed["_meta"] = meta
                    return parsed
            except json.JSONDecodeError:
                pass
            return {"raw_response": content, "_meta": meta}
        except Exception as exc:
            logger.error("Bedrock request failed: %s", exc.__class__.__name__)
            incr("llm_failures")
            incr("llm_failures_total")
            return None


def _invoke_bedrock_converse(
    *,
    region: str,
    model_id: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    import boto3
    from botocore.config import Config

    system = [{"text": item["content"]} for item in messages if item.get("role") == "system"]
    conversation = [
        {
            "role": "assistant" if item.get("role") == "assistant" else "user",
            "content": [{"text": item["content"]}],
        }
        for item in messages
        if item.get("role") != "system"
    ]
    if not conversation:
        raise ValueError("Bedrock conversation requires at least one non-system message")
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
            retries={"max_attempts": 4, "mode": "adaptive"},
        ),
    )
    request: dict[str, Any] = {
        "modelId": model_id,
        "messages": conversation,
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
        },
    }
    if system:
        request["system"] = system
    return client.converse(**request)


_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        _provider = LLMProvider()
    return _provider

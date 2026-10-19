"""
provider.py — thin async adapter for LLM providers.

Design:
  - Single interface regardless of provider (OpenAI / Anthropic / Google).
  - 15 s timeout per call.
  - Retry exactly once on: 429, timeout, 5xx. Never on 4xx auth/validation errors.
  - Returns raw message object; agent loop handles tool_calls extraction.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx


TIMEOUT = float(os.getenv("TIMEOUT_SECONDS", "15"))
_PROVIDER = os.getenv("PROVIDER", "openai").lower()
_MODEL = os.getenv("MODEL", "gpt-4o-mini")

# Statuses that warrant a single retry
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


# Provider configurations
_PROVIDER_CONFIGS = {
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
    },
    "google": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GOOGLE_API_KEY",
        "default_model": "gemini-2.5-flash",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "default_model": "qwen/qwen3.8-27b",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "default_model": "meta-llama/llama-3.3-70b-instruct",
    },
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "key_env": "NVIDIA_API_KEY",
        "default_model": "deepseek-ai/deepseek-v4.1-flash",
    },
}

# ─── OpenAI / Compatible HTTP adapter ─────────────────────────────────────────

async def _openai_compatible_chat(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    timeout: float = TIMEOUT,
) -> dict:
    """Async HTTP call to OpenAI-compatible chat completions endpoint."""
    provider = os.getenv("PROVIDER", "openai").lower()
    cfg = _PROVIDER_CONFIGS.get(provider, _PROVIDER_CONFIGS["openai"])
    
    api_key = os.getenv(cfg["key_env"], "")
    if not api_key:
        raise ProviderError(401, f"Missing API key: {cfg['key_env']}")
        
    model = os.getenv("MODEL") or cfg["default_model"]
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            cfg["url"],
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if resp.status_code not in (200,):
        raise ProviderError(resp.status_code, resp.text, dict(resp.headers))
    return resp.json()

_openai_chat = _openai_compatible_chat


# ─── Anthropic HTTP adapter ───────────────────────────────────────────────────

async def _anthropic_chat(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    timeout: float = TIMEOUT,
) -> dict:
    """Raw async HTTP call to Anthropic messages endpoint."""
    api_key = os.environ["ANTHROPIC_API_KEY"]
    # Convert tool schema to Anthropic format
    ant_tools = []
    if tools:
        for t in tools:
            fn = t["function"]
            ant_tools.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })

    # Anthropic requires system as separate field
    system = ""
    ant_messages = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            ant_messages.append(m)

    payload: dict[str, Any] = {
        "model": os.getenv("MODEL", "claude-3-5-sonnet-20241022"),
        "max_tokens": 4096,
        "messages": ant_messages,
    }
    if system:
        payload["system"] = system
    if ant_tools:
        payload["tools"] = ant_tools

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if resp.status_code not in (200,):
        raise ProviderError(resp.status_code, resp.text, dict(resp.headers))
    # Normalize to OpenAI-like format for the agent loop
    body = resp.json()
    tool_calls = []
    text_content = ""
    for block in body.get("content", []):
        if block.get("type") == "tool_use":
            tool_calls.append({
                "id": block["id"],
                "type": "function",
                "function": {"name": block["name"], "arguments": json.dumps(block["input"])},
            })
        elif block.get("type") == "text":
            text_content += block.get("text", "")

    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": text_content or None,
                "tool_calls": tool_calls or None,
            },
            "finish_reason": body.get("stop_reason", "stop"),
        }]
    }


# ─── Public interface ─────────────────────────────────────────────────────────

class ProviderError(Exception):
    def __init__(self, status: int, body: str, headers: dict | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}
        super().__init__(f"Provider HTTP {status}: {body[:200]}")

    @property
    def is_transient(self) -> bool:
        return self.status in _TRANSIENT_STATUS


async def chat_completion(
    messages: list[dict],
    tools: list[dict] | None = None,
) -> dict:
    """
    Call the configured provider, retrying on transient errors/timeouts.
    For 429 rate limits, waits for token reset window.
    """
    provider = os.getenv("PROVIDER", "openai").lower()
    _call = _anthropic_chat if provider == "anthropic" else _openai_chat

    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            return await asyncio.wait_for(_call(messages, tools), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            if attempt < max_attempts - 1:
                await asyncio.sleep(1.0)
                continue
            raise ProviderError(504, "Timeout after retry")
        except ProviderError as e:
            if e.is_transient and attempt < max_attempts - 1:
                wait_sec = 2.0
                if e.status == 429:
                    # Check retry-after or parse Groq reset
                    retry_header = e.headers.get("retry-after")
                    if retry_header:
                        try:
                            wait_sec = max(2.0, float(retry_header) + 0.5)
                        except ValueError:
                            wait_sec = 15.0
                    else:
                        wait_sec = 15.0
                await asyncio.sleep(wait_sec)
                continue
            raise

    raise ProviderError(0, "Unreachable")


def extract_message(response: dict) -> dict:
    """Get the message dict from a normalized response."""
    return response["choices"][0]["message"]


def extract_tool_calls(message: dict) -> list[dict]:
    """Return list of tool_call dicts, or empty list."""
    return message.get("tool_calls") or []


def extract_text(message: dict) -> str:
    """Return text content of the message, or empty string."""
    return message.get("content") or ""

# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Provider adapters behind one interface. Author: Akshay Dubey

Each adapter takes (system, user, max_tokens, options) and returns a
Completion with text + real token usage reported by the provider.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from . import secrets
from .registry import ModelSpec


@dataclass
class Completion:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    stop_reason: str = ""
    cached_tokens: int = 0
    raw: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)      # rate-limit headers, for the free-tier ledger


class ProviderError(RuntimeError):
    def __init__(self, msg: str, retryable: bool = True, status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(msg)
        self.retryable, self.status, self.retry_after = retryable, status, retry_after


def rate_headers(resp: httpx.Response) -> dict:
    return {k: v for k, v in resp.headers.items() if k.lower().startswith(("x-ratelimit", "retry-after"))}


class Provider:
    async def complete(self, spec: ModelSpec, system: str, user: str, max_tokens: int,
                       *, json_mode: bool = False, web: bool = False, timeout_s: float = 120,
                       messages: list[dict] | None = None) -> Completion:
        """`messages` (alternating user/assistant dicts) replaces `user` for multi-turn calls."""
        raise NotImplementedError

    async def stream(self, spec: ModelSpec, system: str, user: str, max_tokens: int,
                     on_delta: Callable[[str], None], **kw) -> Completion:
        """Like complete(), calling on_delta(text) as the answer arrives. Default: one chunk at the end."""
        c = await self.complete(spec, system, user, max_tokens, **kw)
        if c.text:
            on_delta(c.text)
        return c


async def _sse_lines(resp: httpx.Response):
    async for line in resp.aiter_lines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue


def _raise_for(resp: httpx.Response, name: str) -> None:
    if resp.status_code < 400:
        return
    retryable = resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500
    ra = None
    try:
        ra = float(resp.headers.get("retry-after", "")) if resp.headers.get("retry-after") else None
    except ValueError:
        ra = None
    raise ProviderError(f"{name} HTTP {resp.status_code}: {resp.text[:300]}", retryable=retryable,
                        status=resp.status_code, retry_after=ra)


class AnthropicProvider(Provider):
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def complete(self, spec, system, user, max_tokens, *, json_mode=False, web=False, timeout_s=120,
                       messages=None):
        msgs = [dict(m) for m in (messages or [{"role": "user", "content": user}])]
        if messages:
            # Cache the whole conversation prefix: on the next turn everything up
            # to here is billed at the cached-input rate. Big saver for agents.
            last = msgs[-1]
            last["content"] = [{"type": "text", "text": last["content"], "cache_control": {"type": "ephemeral"}}]
        body: dict = {
            "model": spec.model,
            "max_tokens": max_tokens,
            # cache_control lets repeated system prompts (skills) be served from
            # the provider's prompt cache at a reduced input price.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": msgs,
        }
        if web and "web" in spec.capabilities:
            body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
        headers = {**secrets.anthropic_headers(secrets.get(spec.api_key_env)), "content-type": "application/json"}
        t0 = time.perf_counter()
        r = await self.client.post(spec.base_url or self.URL, json=body, headers=headers, timeout=timeout_s)
        _raise_for(r, "anthropic")
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage", {})
        return Completion(text, u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                          + u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0),
                          (time.perf_counter() - t0) * 1000, d.get("stop_reason", ""),
                          u.get("cache_read_input_tokens", 0), d)


    async def stream(self, spec, system, user, max_tokens, on_delta, *, json_mode=False, web=False,
                     timeout_s=120, messages=None):
        msgs = [dict(m) for m in (messages or [{"role": "user", "content": user}])]
        if messages:
            # Same cache breakpoint as complete(): the conversation prefix is billed
            # at the cached-input rate on the next turn.
            last = msgs[-1]
            last["content"] = [{"type": "text", "text": last["content"], "cache_control": {"type": "ephemeral"}}]
        body = {"model": spec.model, "max_tokens": max_tokens, "stream": True,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "messages": msgs}
        if web and "web" in spec.capabilities:
            body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
        headers = {**secrets.anthropic_headers(secrets.get(spec.api_key_env)), "content-type": "application/json"}
        t0 = time.perf_counter()
        parts, tin, tout, cached, stop = [], 0, 0, 0, ""
        async with self.client.stream("POST", spec.base_url or self.URL, json=body, headers=headers,
                                      timeout=timeout_s) as r:
            if r.status_code >= 400:
                await r.aread()
                _raise_for(r, "anthropic")
            async for ev in _sse_lines(r):
                t = ev.get("type")
                if t == "message_start":
                    u = (ev.get("message") or {}).get("usage") or {}
                    cached = u.get("cache_read_input_tokens", 0) or 0
                    tin = (u.get("input_tokens", 0) or 0) + (u.get("cache_creation_input_tokens", 0) or 0) + cached
                elif t == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                    txt = ev["delta"].get("text", "")
                    parts.append(txt)
                    on_delta(txt)
                elif t == "message_delta":
                    tout = ((ev.get("usage") or {}).get("output_tokens")) or tout
                    stop = (ev.get("delta") or {}).get("stop_reason") or stop
                elif t == "error":
                    raise ProviderError(f"anthropic stream error: {str(ev.get('error'))[:300]}")
        return Completion("".join(parts), tin, tout, (time.perf_counter() - t0) * 1000, stop, cached, {})


class OpenAIProvider(Provider):
    """OpenAI and any OpenAI-compatible endpoint (Gemini, OpenRouter, Ollama, vLLM, Azure...)."""
    URL = "https://api.openai.com/v1"

    def __init__(self, client: httpx.AsyncClient, compatible: bool = False):
        self.client, self.compatible = client, compatible

    async def _headers(self, spec, base: str) -> dict:
        """Request headers for one call. Async so token-exchange providers can refresh."""
        headers = {"content-type": "application/json"}
        key = secrets.get(spec.api_key_env) if spec.api_key_env else ""
        if key:
            headers["authorization"] = f"Bearer {key}"
        if "openrouter.ai" in base:
            headers["HTTP-Referer"] = "http://localhost"
            headers["X-Title"] = "Promptaro"
        return headers

    async def complete(self, spec, system, user, max_tokens, *, json_mode=False, web=False, timeout_s=120,
                       messages=None):
        base = (spec.base_url or self.URL).rstrip("/")
        convo = messages or [{"role": "user", "content": user}]
        body: dict = {"model": spec.model, "messages": [{"role": "system", "content": system}, *convo]}
        # Official OpenAI uses max_completion_tokens; most compatible servers still take max_tokens.
        body["max_tokens" if self.compatible else "max_completion_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = await self._headers(spec, base)
        t0 = time.perf_counter()
        r = await self.client.post(f"{base}/chat/completions", json=body, headers=headers, timeout=timeout_s)
        _raise_for(r, spec.provider)
        d = r.json()
        ch = (d.get("choices") or [{}])[0]
        u = d.get("usage", {}) or {}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return Completion(ch.get("message", {}).get("content") or "", u.get("prompt_tokens", 0),
                          u.get("completion_tokens", 0), (time.perf_counter() - t0) * 1000,
                          ch.get("finish_reason", ""), cached or 0, d, rate_headers(r))


    async def stream(self, spec, system, user, max_tokens, on_delta, *, json_mode=False, web=False,
                     timeout_s=120, messages=None):
        if json_mode:                                  # structured output: nothing useful to show early
            return await Provider.stream(self, spec, system, user, max_tokens, on_delta, json_mode=json_mode,
                                         web=web, timeout_s=timeout_s, messages=messages)
        base = (spec.base_url or self.URL).rstrip("/")
        convo = messages or [{"role": "user", "content": user}]
        body: dict = {"model": spec.model, "messages": [{"role": "system", "content": system}, *convo],
                      "stream": True, "stream_options": {"include_usage": True}}
        body["max_tokens" if self.compatible else "max_completion_tokens"] = max_tokens
        headers = await self._headers(spec, base)
        t0 = time.perf_counter()
        parts, tin, tout, cached, stop = [], 0, 0, 0, ""
        async with self.client.stream("POST", f"{base}/chat/completions", json=body, headers=headers,
                                      timeout=timeout_s) as r:
            if r.status_code >= 400:
                await r.aread()
                _raise_for(r, spec.provider)
            hdrs = rate_headers(r)
            async for ev in _sse_lines(r):
                if ev.get("error"):
                    err = ev["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    code = err.get("code") if isinstance(err, dict) else None
                    raise ProviderError(f"{spec.provider} HTTP {code or 'error'}: {msg[:300]}")
                for ch in ev.get("choices") or []:
                    txt = (ch.get("delta") or {}).get("content") or ""
                    if txt:
                        parts.append(txt)
                        on_delta(txt)
                    stop = ch.get("finish_reason") or stop
                u = ev.get("usage")
                if u:
                    tin, tout = u.get("prompt_tokens", tin), u.get("completion_tokens", tout)
                    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        text = "".join(parts)
        if not tout:                                     # some servers omit usage when streaming
            tin, tout = tin or (len(system) + sum(len(str(m.get("content", ""))) for m in convo)) // 4, len(text) // 4
        return Completion(text, tin, tout, (time.perf_counter() - t0) * 1000, stop, cached, {}, hdrs)


class OfflineProvider(Provider):
    """No-AI fallback: keeps the system usable (and testable) with no keys."""

    async def complete(self, spec, system, user, max_tokens, *, json_mode=False, web=False, timeout_s=120,
                       messages=None):
        msg = ("[offline] No AI model is configured or reachable, so this request was not sent to a model. "
               "Connect an account (Sign in with OpenRouter) or start Ollama to enable AI.")
        return Completion(msg, len(user) // 4, len(msg) // 4, 0.1, "offline")


def build_providers(client: httpx.AsyncClient) -> dict[str, Provider]:
    return {
        "anthropic": AnthropicProvider(client),
        "openai": OpenAIProvider(client, compatible=False),
        "openai_compatible": OpenAIProvider(client, compatible=True),
        "offline": OfflineProvider(),
    }

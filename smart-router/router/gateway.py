# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Coding-tools gateway: point Claude Code, Aider, Cline, Continue or OpenCode here. Author: Akshay Dubey

  POST /v1/messages               Anthropic Messages API (Claude Code), streaming, tools, images
  POST /v1/messages/count_tokens  local estimate, no model call
  POST /v1/chat/completions       OpenAI Chat Completions (Aider, Cline, Continue, OpenCode, ...)
  GET  /v1/models                 what's available (Anthropic or OpenAI list format)

Every request is routed like a chat message: difficulty is scored locally (no
model call), the model name the tool asks for is a hint (haiku = quick
housekeeping, opus = hardest work), and the cheapest model that can do the job
and still has free allowance left answers. A model that refuses (rate limit,
outage, too large) is skipped at once and the next one answers, before any text
has been sent, so the tool never sees the failure.

Security: listens only where the app listens (127.0.0.1) and every request
must carry the gateway token. The token is a gateway credential, so a client
like Claude Code never sends a subscription login through here.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets as pysecrets
import time
import uuid
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import catalog, quota, secrets
from .classifier import classify
from .providers import ProviderError, rate_headers
from .selector import route

api = APIRouter()
TOKEN_KEY = "ROUTER_GATEWAY_TOKEN"
MAX_CANDIDATES = 6
MAX_OUT = 16_384
MAX_WAIT_S = 60          # wait this long for a per-minute free allowance rather than refusing
_client: httpx.AsyncClient | None = None


def gateway_token() -> str:
    t = secrets.get(TOKEN_KEY)
    if not t:
        t = "pro-" + pysecrets.token_urlsafe(32)
        secrets.set(TOKEN_KEY, t)
    return t


def rotate_token() -> str:
    secrets.delete(TOKEN_KEY)
    return gateway_token()


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=15))
    return _client


def _authorized(request: Request) -> bool:
    supplied = request.headers.get("x-api-key") or ""
    auth = request.headers.get("authorization") or ""
    if not supplied and auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied.encode(), gateway_token().encode())


def _anthropic_error(status: int, kind: str, msg: str) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": kind, "message": msg}}, status_code=status)


def _openai_error(status: int, kind: str, msg: str) -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": kind, "code": status}}, status_code=status)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def estimate_tokens(obj) -> int:
    return max(1, int(len(json.dumps(obj, ensure_ascii=False)) / 3.6))


# ============================================================ translation
def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _image_url(block: dict) -> str | None:
    src = block.get("source") or {}
    if src.get("type") == "base64":
        return f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
    if src.get("type") == "url":
        return src.get("url")
    return None


def _clean_schema(schema) -> dict:
    s = dict(schema or {"type": "object", "properties": {}})
    s.pop("$schema", None)
    s.setdefault("type", "object")
    return s


def anthropic_to_openai(body: dict, short_ids: bool = False) -> dict:
    """Anthropic Messages request -> OpenAI Chat Completions request (without the model)."""
    def tid(i: str) -> str:
        if not short_ids or re.fullmatch(r"[A-Za-z0-9]{9}", i or ""):
            return i
        h = hashlib.sha256((i or "").encode()).hexdigest()
        return "".join(c for c in h if c.isalnum())[:9]

    msgs: list[dict] = []
    system = _text(body.get("system")) if not isinstance(body.get("system"), str) else body["system"]
    if system:
        msgs.append({"role": "system", "content": system})
    for m in body.get("messages") or []:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        blocks = [b for b in content or [] if isinstance(b, dict)]
        if role == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [{"id": tid(b.get("id", "")), "type": "function",
                      "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            out = {"role": "assistant", "content": text or None}
            if calls:
                out["tool_calls"] = calls
            if text or calls:
                msgs.append(out)
            continue
        # user: tool results first (they answer the previous assistant turn), then the rest
        parts: list[dict] = []
        for b in blocks:
            t = b.get("type")
            if t == "tool_result":
                c = b.get("content")
                txt = c if isinstance(c, str) else _text(c)
                if isinstance(c, list) and any(x.get("type") == "image" for x in c if isinstance(x, dict)):
                    txt += "\n[image omitted]"
                if b.get("is_error"):
                    txt = "Error: " + txt
                msgs.append({"role": "tool", "tool_call_id": tid(b.get("tool_use_id", "")), "content": txt or "(empty)"})
            elif t == "text" and b.get("text"):
                parts.append({"type": "text", "text": b["text"]})
            elif t == "image":
                url = _image_url(b)
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            elif t == "document":
                parts.append({"type": "text", "text": "[document attached; not supported by this model]"})
        if parts:
            if all(p["type"] == "text" for p in parts):
                msgs.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
            else:
                msgs.append({"role": "user", "content": parts})
    out: dict = {"messages": msgs}
    tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                "parameters": _clean_schema(t.get("input_schema"))}}
             for t in body.get("tools") or [] if isinstance(t, dict) and t.get("name") and "input_schema" in t]
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice") or {}
        kind = tc.get("type") if isinstance(tc, dict) else None
        if kind == "any":
            out["tool_choice"] = "required"
        elif kind == "tool":
            out["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
        elif kind == "none":
            out["tool_choice"] = "none"
    for k_in, k_out in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(k_in) is not None:
            out[k_out] = body[k_in]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"][:4]
    return out


def _stop_reason(finish: str | None, used_tools: bool) -> str:
    if used_tools and finish in (None, "", "stop", "tool_calls", "function_call"):
        return "tool_use"
    return {"length": "max_tokens", "tool_calls": "tool_use"}.get(finish or "", "end_turn")


def _json_args(raw: str) -> dict:
    """Tool arguments as an object, repairing the usual small mistakes."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    for cand in (raw, raw[raw.find("{"): raw.rfind("}") + 1] if "{" in raw else ""):
        try:
            v = json.loads(cand)
            return v if isinstance(v, dict) else {"value": v}
        except (json.JSONDecodeError, ValueError):
            continue
    return {}


def openai_to_anthropic(resp: dict, model: str) -> dict:
    ch = (resp.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    calls = msg.get("tool_calls") or []
    for c in calls:
        fn = c.get("function") or {}
        content.append({"type": "tool_use", "id": c.get("id") or f"toolu_{uuid.uuid4().hex[:20]}",
                        "name": fn.get("name", ""), "input": _json_args(fn.get("arguments", ""))})
    u = resp.get("usage") or {}
    return {"id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": _stop_reason(ch.get("finish_reason"), bool(calls)),
            "stop_sequence": None,
            "usage": {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}}


class StreamTranslator:
    """OpenAI chat-completion chunks in, Anthropic message events out.

    Text streams as it arrives. Tool calls are collected and sent as one block
    each with arguments that parse, so a slightly malformed reply from a free
    model doesn't break the client."""

    def __init__(self, model: str, input_tokens: int):
        self.model, self.input_tokens = model, input_tokens
        self.index = -1
        self.text_open = False
        self.tools: dict[int, dict] = {}      # openai tool index -> {id, name, args}
        self.order: list[int] = []
        self.finish: str | None = None
        self.output_tokens = 0
        self.text_chars = 0

    def start(self) -> str:
        return sse("message_start", {"type": "message_start", "message": {
            "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": 0}}})

    def _close_text(self) -> list[str]:
        if not self.text_open:
            return []
        self.text_open = False
        return [sse("content_block_stop", {"type": "content_block_stop", "index": self.index})]

    def feed(self, chunk: dict) -> list[str]:
        out: list[str] = []
        u = chunk.get("usage")
        if u:
            self.input_tokens = u.get("prompt_tokens") or self.input_tokens
            self.output_tokens = u.get("completion_tokens") or self.output_tokens
        for ch in chunk.get("choices") or []:
            d = ch.get("delta") or {}
            txt = d.get("content")
            if txt:
                if not self.text_open:
                    self.index += 1
                    self.text_open = True
                    out.append(sse("content_block_start", {"type": "content_block_start", "index": self.index,
                                                           "content_block": {"type": "text", "text": ""}}))
                self.text_chars += len(txt)
                out.append(sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                       "delta": {"type": "text_delta", "text": txt}}))
            for tc in d.get("tool_calls") or []:
                i = tc.get("index", len(self.order))
                t = self.tools.get(i)
                if t is None:
                    t = self.tools[i] = {"id": "", "name": "", "args": ""}
                    self.order.append(i)
                if tc.get("id"):
                    t["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    t["name"] += fn["name"] if not t["name"] or fn["name"] != t["name"] else ""
                if fn.get("arguments"):
                    t["args"] += fn["arguments"]
            if ch.get("finish_reason"):
                self.finish = ch["finish_reason"]
        return out

    def end(self) -> list[str]:
        out = self._close_text()
        for i in self.order:
            t = self.tools[i]
            if not t["name"]:
                continue
            self.index += 1
            tool_id = t["id"] or f"toolu_{uuid.uuid4().hex[:20]}"
            out.append(sse("content_block_start", {"type": "content_block_start", "index": self.index,
                                                   "content_block": {"type": "tool_use", "id": tool_id,
                                                                     "name": t["name"], "input": {}}}))
            out.append(sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                   "delta": {"type": "input_json_delta",
                                                             "partial_json": json.dumps(_json_args(t["args"]))}}))
            out.append(sse("content_block_stop", {"type": "content_block_stop", "index": self.index}))
        used_tools = any(self.tools[i]["name"] for i in self.order)
        if not self.output_tokens:
            self.output_tokens = max(1, (self.text_chars + sum(len(self.tools[i]["args"]) for i in self.order)) // 4)
        out.append(sse("message_delta", {"type": "message_delta",
                                         "delta": {"stop_reason": _stop_reason(self.finish, used_tools),
                                                   "stop_sequence": None},
                                         "usage": {"input_tokens": self.input_tokens,
                                                   "output_tokens": self.output_tokens}}))
        out.append(sse("message_stop", {"type": "message_stop"}))
        return out


# ================================================================ routing
def _hint(model_name: str) -> tuple[str, float | None, float | None]:
    """(prefer, complexity floor, complexity ceiling) from the model name the tool asked for."""
    n = (model_name or "").lower()
    if "haiku" in n or "mini" in n or "small" in n or "fast" in n:
        return "fast", None, 0.25            # titles, summaries, quick checks
    if "opus" in n or "fable" in n or "mythos" in n or n.endswith("-best"):
        return "best", 0.7, None
    return "balanced", None, None


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            t = c if isinstance(c, str) else " ".join(
                b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")
            if t.strip():
                return t
    return ""


_sticky: dict[str, tuple[float, str]] = {}       # conversation fingerprint -> (expires, model_id)
_STICKY_TTL = 3600                                # keep a tool's conversation on one model ~1h


def _stick_key(messages: list[dict], tools: bool) -> str:
    """A tool's conversation is identified by its first user message, so every turn of that
    conversation prefers the model it started on — better prompt-cache hits and consistency."""
    first = next((m for m in messages if m.get("role") == "user"), None)
    text = first.get("content") if first else ""
    if not isinstance(text, str):
        text = json.dumps(text)[:2000]
    return hashlib.sha256((text[:2000] + ("|t" if tools else "")).encode()).hexdigest()[:16]


def _sticky_pick(key: str, model_id: str) -> None:
    now = time.time()
    for k in [k for k, (exp, _) in _sticky.items() if exp <= now]:
        _sticky.pop(k, None)
    _sticky[key] = (now + _STICKY_TTL, model_id)


def plan(router, *, messages: list[dict], est_in: int, est_out: int, tools: bool, images: bool,
         requested: str) -> list:
    """Models to try, best first. An exact model name is a hint: spend, privacy,
    capability and quota rules apply to it the same as to any routed choice."""
    reg = router.registry
    settings = catalog.get_settings()
    mode = settings.get("free_models", "chat")
    free_only = settings.get("gateway_spend", "free") == "free"
    daily_left = router._daily_budget_left()
    need = {"tools"} if tools else set()
    free_ok = catalog.FREE.available()
    usable_ids = {m.id for m in reg.usable()}

    def usable_for_gateway(m) -> bool:
        if m.provider == "offline" or m.id not in usable_ids:
            return False
        is_free = bool(m.extra.get("free")) or (m.price_in == 0 and m.price_out == 0)
        if free_only and not is_free:
            return False                                        # fail closed: never silently spend
        if not is_free and m.cost(est_in, est_out) > daily_left:
            return False                                        # daily budget reached
        if m.extra.get("free") and (
                mode == "off"                                    # free models disabled entirely
                or (mode != "all" and m.extra.get("trains"))     # private work: no trainers
                or (not m.extra.get("quota") and not free_ok)):  # shared :free pool exhausted
            return False
        if need - m.capabilities or est_in > m.context_window * 0.9:
            return False
        if m.extra.get("quota") and not quota.LEDGER.allow(m, est_in + est_out)[0]:
            return False
        return True

    exact = reg.models.get(requested)
    if exact and usable_for_gateway(exact):
        return [exact]
    text = _last_user_text(messages)
    prof = classify(text[:20_000])
    prefer, floor, ceil = _hint(requested)
    if tools:
        prof.task_type = "coding"
    if floor is not None:
        prof.complexity = max(prof.complexity, floor)
    if ceil is not None:
        prof.complexity = min(prof.complexity, ceil)
    prof.est_input_tokens = est_in
    if images:
        prof.needs.add("vision")
    d = route(reg, router.store, prof, prefer=prefer, allow_free=mode != "off", private=mode != "all",
              require=need, est_output=est_out)
    seen, out = set(), []
    rest = sorted((c for c in d.considered), key=lambda c: (not c.clears_bar, -c.score))
    for c in [d.primary, *d.ladder, *rest]:
        if c.model.id in seen or not usable_for_gateway(c.model):
            continue
        seen.add(c.model.id)
        out.append(c.model)
    out = out[:MAX_CANDIDATES]
    hit = _sticky.get(_stick_key(messages, tools))
    if hit and hit[0] > time.time():
        for i, m in enumerate(out):                    # stay on this conversation's model when it still qualifies
            if m.id == hit[1]:
                out.insert(0, out.pop(i))
                break
    return out


async def plan_or_wait(router, **kw) -> list:
    """plan(); if nothing can take the request only because per-minute free allowances are used up,
    wait for the soonest one (up to MAX_WAIT_S) instead of refusing."""
    try:
        c = plan(router, **kw)
    except RuntimeError:
        c = []
    if c:
        return c
    est = kw["est_in"] + kw["est_out"]
    settings = catalog.get_settings()
    mode = settings.get("free_models", "chat")
    free_only = settings.get("gateway_spend", "free") == "free"
    need = {"tools"} if kw.get("tools") else set()
    waits = []
    for m in router.registry.usable():
        if not m.extra.get("quota") or (need - m.capabilities) or (mode != "all" and m.extra.get("trains")) \
                or mode == "off" or kw["est_in"] > m.context_window * 0.9:
            continue
        if free_only and not (m.extra.get("free") or (m.price_in == 0 and m.price_out == 0)):
            continue
        w = quota.LEDGER.wait_for(m, est)
        if w is not None:
            waits.append(w)
    if waits and min(waits) <= MAX_WAIT_S:
        await asyncio.sleep(min(waits) + 0.25)
        try:
            return plan(router, **kw)
        except RuntimeError:
            return []
    return []


# ================================================================ upstream
def _openai_target(spec) -> tuple[str, dict]:
    base = (spec.base_url or "https://api.openai.com/v1").rstrip("/")
    headers = {"content-type": "application/json"}
    key = secrets.get(spec.api_key_env) if spec.api_key_env else ""
    if key:
        headers["authorization"] = f"Bearer {key}"
    if "openrouter.ai" in base:
        headers["HTTP-Referer"] = "http://localhost"
        headers["X-Title"] = "Promptaro"
    return f"{base}/chat/completions", headers


async def _open(method_url: str, headers: dict, body: dict, stream: bool) -> httpx.Response:
    c = _http()
    req = c.build_request("POST", method_url, json=body, headers=headers)
    resp = await c.send(req, stream=stream)
    if resp.status_code >= 400:
        await resp.aread()
        ra = resp.headers.get("retry-after")
        try:
            ra_s = float(ra) if ra else None
        except ValueError:
            ra_s = None
        text = resp.text[:400]
        await resp.aclose()
        retryable = resp.status_code in (408, 409, 425, 429, 529) or resp.status_code >= 500
        raise ProviderError(f"HTTP {resp.status_code}: {text}", retryable=retryable,
                            status=resp.status_code, retry_after=ra_s)
    return resp


def _failed(router, spec, err: Exception, tried: list) -> None:
    if not quota.after_error(spec, err):
        router.registry.breaker.record(spec.id, False)
    transient = getattr(err, "retryable", None)
    if transient is None:
        status = getattr(err, "status", None)
        transient = status is None or status in (408, 409, 425, 429, 529) or status >= 500
    tried.append((f"{spec.extra.get('name', spec.id)}: {str(err)[:160]}", transient))


def _gave_up(tried: list) -> bool:
    """Two providers rejecting the request itself (not rate limits) means retrying
    more candidates almost certainly won't help — the request is the problem."""
    return sum(1 for _, tr in tried if not tr) >= 2


def _all_failed(tried: list, anthropic: bool = True):
    """Every candidate failed. Rate limits/outages: ask the client to retry. Anything else (bad key,
    rejected request): say so plainly, since retrying won't help."""
    why = "; ".join(t for t, _ in tried[:4])
    if any(tr for _, tr in tried):
        msg = "Every model that could take this request is busy or rate-limited just now: " + why
        return _anthropic_error(529, "overloaded_error", msg) if anthropic else _openai_error(503, "all_busy", msg)
    msg = ("Promptaro: the connected models refused this request (check the keys in Promptaro → Connections): "
           + why)
    return _anthropic_error(400, "invalid_request_error", msg) if anthropic else _openai_error(400, "all_refused", msg)


def _account(router, spec, tin: int, tout: int, headers: dict | None = None, res: dict | None = None,
             stick: str | None = None) -> None:
    if res is not None:
        quota.LEDGER.settle(res, spec, tin + tout, headers)
    else:
        quota.after_call(spec, SimpleNamespace(tokens_in=tin, tokens_out=tout, headers=headers or {}))
    if stick:
        _sticky_pick(stick, spec.id)
    router.registry.breaker.record(spec.id, True)
    router.notify_usage(spec, tin, tout, "gateway")


def _router(request: Request):
    return request.app.state.router


def _model_headers(spec) -> dict:
    """Which model actually answered. Sent under both names: Promptaro-era clients read the
    new header, tools written for Tillwright keep reading the old one."""
    return {"x-promptaro-model": spec.id, "x-promptaro-via": spec.extra.get("via", ""),
            "x-tillwright-model": spec.id, "x-tillwright-via": spec.extra.get("via", "")}


# ================================================================ endpoints
@api.get("/v1/models")
async def models(request: Request):
    if not _authorized(request):
        return _anthropic_error(401, "authentication_error", "Missing or wrong Promptaro gateway token.")
    reg = _router(request).registry
    settings = catalog.get_settings()
    free_only = settings.get("gateway_spend", "free") == "free"
    mode = settings.get("free_models", "chat")
    usable = [m for m in reg.usable() if m.provider != "offline"]
    if free_only:
        usable = [m for m in usable if m.extra.get("free") or (m.price_in == 0 and m.price_out == 0)]
    if mode != "all":
        usable = [m for m in usable if not (m.extra.get("free") and m.extra.get("trains"))]
    ids = [("promptaro-auto", "Promptaro Auto (picks the model)"),
           ("promptaro-fast", "Promptaro Fast (quick, free when possible)"),
           ("promptaro-best", "Promptaro Best (strongest available)"),
           # Legacy Tillwright aliases: tools configured before the rename keep working.
           ("tillwright-auto", "Tillwright Auto → Promptaro Auto"),
           ("tillwright-fast", "Tillwright Fast → Promptaro Fast"),
           ("tillwright-best", "Tillwright Best → Promptaro Best")] + \
        [(m.id, m.extra.get("name", m.id)) for m in usable]
    if request.headers.get("anthropic-version"):
        data = [{"type": "model", "id": i, "display_name": n, "created_at": "2026-01-01T00:00:00Z"} for i, n in ids]
        return {"data": data, "has_more": False, "first_id": data[0]["id"], "last_id": data[-1]["id"]}
    return {"object": "list", "data": [{"id": i, "object": "model", "owned_by": "promptaro"} for i, _ in ids]}


@api.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    if not _authorized(request):
        return _anthropic_error(401, "authentication_error", "Missing or wrong Promptaro gateway token.")
    body = await request.json()
    return {"input_tokens": estimate_tokens({k: body.get(k) for k in ("system", "messages", "tools")})}


@api.post("/v1/messages")
async def messages(request: Request):
    if not _authorized(request):
        return _anthropic_error(401, "authentication_error", "Missing or wrong Promptaro gateway token. "
                                "Start your tool with Promptaro's launcher, or copy the token from "
                                "Promptaro → Coding tools.")
    router = _router(request)
    body = await request.json()
    requested = str(body.get("model") or "promptaro-auto")
    est_in = estimate_tokens({k: body.get(k) for k in ("system", "messages", "tools")})
    want_out = int(body.get("max_tokens") or 4096)
    has_images = any(isinstance(b, dict) and b.get("type") == "image" for m in body.get("messages") or []
                     if isinstance(m.get("content"), list) for b in m["content"])
    cands = await plan_or_wait(router, messages=body.get("messages") or [], est_in=est_in,
                               est_out=min(want_out, 1024), tools=bool(body.get("tools")), images=has_images,
                               requested=requested)
    if not cands:
        # Not a momentary overload, so don't invite the client to retry: say what to do.
        return _anthropic_error(400, "invalid_request_error", "Promptaro: no connected model can take this request "
                                "right now. The free models' limits may be used up or the request may be too large "
                                "for them. Add a free provider in Promptaro → Connections, allow paid models in "
                                "Coding tools, or try again later.")
    stream = bool(body.get("stream"))
    stick = _stick_key(body.get("messages") or [], bool(body.get("tools")))
    tried: list = []      # actual provider refusals
    skipped: list = []    # models whose free allowance ran out between planning and sending
    for spec in cands:
        res = None
        if spec.extra.get("quota"):
            ok, why, res = quota.LEDGER.begin(spec, est_in + min(want_out, 1024))
            if not ok:
                skipped.append(f"{spec.extra.get('name', spec.id)}: {why}")
                continue
        try:
            if spec.provider == "anthropic":
                out = dict(body, model=spec.model)
                url = (spec.base_url or "https://api.anthropic.com") .rstrip("/")
                url = url if url.endswith("/v1/messages") else url + "/v1/messages"
                hdrs = {**secrets.anthropic_headers(secrets.get(spec.api_key_env)), "content-type": "application/json"}
                if request.headers.get("anthropic-beta"):
                    hdrs["anthropic-beta"] = request.headers["anthropic-beta"]
                resp = await _open(url, hdrs, out, stream)
                if stream:
                    return _anthropic_passthrough(router, spec, resp, est_in, res, stick)
                try:
                    d = resp.json()
                except ValueError:
                    quota.LEDGER.abandon(res)
                    _failed(router, spec, ProviderError("anthropic returned a non-JSON response",
                                                      retryable=False), tried)
                    continue
                u = d.get("usage") or {}
                _account(router, spec, u.get("input_tokens", est_in), u.get("output_tokens", 0),
                         rate_headers(resp), res, stick)
                headers = _model_headers(spec)
                return JSONResponse(d, headers=headers)
            url, hdrs = _openai_target(spec)
            ob = anthropic_to_openai(body, short_ids=spec.extra.get("quota") in ("mistral", "codestral"))
            ob["model"] = spec.model
            room = max(256, spec.context_window - est_in - 256)
            ob["max_tokens" if spec.provider != "openai" else "max_completion_tokens"] = max(1, min(want_out, MAX_OUT, room))
            if stream:
                ob["stream"] = True
                ob["stream_options"] = {"include_usage": True}
            resp = await _open(url, hdrs, ob, stream)
        except (ProviderError, httpx.HTTPError) as e:
            quota.LEDGER.abandon(res)
            _failed(router, spec, e, tried)
            if _gave_up(tried):
                break
            continue
        headers = {**_model_headers(spec), "x-promptaro-est-tokens": str(est_in)}
        if not stream:
            try:
                data = resp.json()
            except ValueError:
                quota.LEDGER.abandon(res)
                _failed(router, spec, ProviderError("model returned a non-JSON response", retryable=False), tried)
                continue
            msg = openai_to_anthropic(data, requested)
            _account(router, spec, msg["usage"]["input_tokens"] or est_in, msg["usage"]["output_tokens"],
                     rate_headers(resp), res, stick)
            return JSONResponse(msg, headers=headers)
        return StreamingResponse(_translate_stream(router, spec, resp, requested, est_in, res, stick),
                                 media_type="text/event-stream", headers={**headers, "cache-control": "no-cache"})
    return _all_failed(tried or [(s, True) for s in skipped])


async def _translate_stream(router, spec, resp: httpx.Response, model: str, est_in: int, res=None, stick=None):
    tr = StreamTranslator(model, est_in)
    hdrs = rate_headers(resp)
    try:
        yield tr.start()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise ProviderError(str(chunk["error"])[:300])
            for ev in tr.feed(chunk):
                yield ev
        for ev in tr.end():
            yield ev
    except Exception as e:                                   # mid-answer failure: tell the client clearly
        yield sse("error", {"type": "error", "error": {"type": "api_error",
                                                      "message": f"{spec.extra.get('name', spec.id)} stopped mid-answer: "
                                                                 f"{str(e)[:200]}. Send the message again."}})
    finally:
        await resp.aclose()
        _account(router, spec, tr.input_tokens, tr.output_tokens, hdrs, res, stick)


def _anthropic_passthrough(router, spec, resp: httpx.Response, est_in: int, res=None, stick=None):
    headers = _model_headers(spec)

    async def pipe():
        tin, tout = est_in, 0
        try:
            async for line in resp.aiter_lines():
                if line.startswith("data:") and '"usage"' in line:
                    try:
                        ev = json.loads(line[5:])
                        u = (ev.get("message") or {}).get("usage") or ev.get("usage") or {}
                        tin = u.get("input_tokens", tin) or tin
                        tout = u.get("output_tokens", tout) or tout
                    except json.JSONDecodeError:
                        pass
                yield line + "\n"
        finally:
            await resp.aclose()
            _account(router, spec, tin, tout, None, res, stick)
    return StreamingResponse(pipe(), media_type="text/event-stream", headers={**headers, "cache-control": "no-cache"})


@api.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not _authorized(request):
        return _openai_error(401, "authentication_error", "Missing or wrong Promptaro gateway token.")
    router = _router(request)
    body = await request.json()
    requested = str(body.get("model") or "promptaro-auto")
    msgs = body.get("messages") or []
    est_in = estimate_tokens({"m": msgs, "t": body.get("tools")})
    want_out = int(body.get("max_tokens") or body.get("max_completion_tokens") or 4096)
    images = any(isinstance(p, dict) and p.get("type") == "image_url" for m in msgs
                 if isinstance(m.get("content"), list) for p in m["content"])
    cands = [c for c in await plan_or_wait(router, messages=msgs, est_in=est_in, est_out=min(want_out, 1024),
                                           tools=bool(body.get("tools")), images=images, requested=requested)
             if c.provider in ("openai", "openai_compatible")]
    if not cands:
        return _openai_error(503, "no_model", "No connected model can take this request right now. Add a free "
                             "provider or a key in Promptaro → Connections.")
    stream = bool(body.get("stream"))
    stick = _stick_key(msgs, bool(body.get("tools")))
    tried: list = []
    skipped: list = []
    for spec in cands:
        res = None
        if spec.extra.get("quota"):
            ok, why, res = quota.LEDGER.begin(spec, est_in + min(want_out, 1024))
            if not ok:
                skipped.append(f"{spec.extra.get('name', spec.id)}: {why}")
                continue
        url, hdrs = _openai_target(spec)
        ob = {k: v for k, v in body.items() if k not in ("max_tokens", "max_completion_tokens")}
        ob["model"] = spec.model
        room = max(256, spec.context_window - est_in - 256)
        ob["max_tokens" if spec.provider != "openai" else "max_completion_tokens"] = max(1, min(want_out, MAX_OUT, room))
        if stream:
            ob["stream_options"] = {"include_usage": True}
        try:
            resp = await _open(url, hdrs, ob, stream)
        except (ProviderError, httpx.HTTPError) as e:
            quota.LEDGER.abandon(res)
            _failed(router, spec, e, tried)
            if _gave_up(tried):
                break
            continue
        headers = _model_headers(spec)
        if not stream:
            try:
                d = resp.json()
            except ValueError:
                quota.LEDGER.abandon(res)
                _failed(router, spec, ProviderError("model returned a non-JSON response", retryable=False), tried)
                continue
            u = d.get("usage") or {}
            _account(router, spec, u.get("prompt_tokens", est_in), u.get("completion_tokens", 0),
                     rate_headers(resp), res, stick)
            d["model"] = requested
            return JSONResponse(d, headers=headers)

        async def pipe(spec=spec, resp=resp, res=res):
            tin, tout = est_in, 0
            hdr = rate_headers(resp)
            try:
                async for line in resp.aiter_lines():
                    if line.startswith("data:") and '"usage"' in line:
                        try:
                            u = json.loads(line[5:]).get("usage") or {}
                            tin, tout = u.get("prompt_tokens", tin) or tin, u.get("completion_tokens", tout) or tout
                        except json.JSONDecodeError:
                            pass
                    yield line + "\n"
            finally:
                await resp.aclose()
                _account(router, spec, tin, tout, hdr, res, stick)
        return StreamingResponse(pipe(), media_type="text/event-stream", headers={**headers, "cache-control": "no-cache"})
    return _all_failed(tried or [(s, True) for s in skipped], anthropic=False)


def client_setup(base_url: str) -> dict:
    """How to point each coding tool at the gateway."""
    t = gateway_token()
    return {
        "base_url": base_url, "token": t,
        "claude_code": {"env": {"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_AUTH_TOKEN": t,
                                "ANTHROPIC_API_KEY": ""}},
        "openai_compatible": {"base_url": f"{base_url}/v1", "api_key": t, "model": "promptaro-auto"},
        "checked": time.strftime("%Y-%m-%d"),
    }

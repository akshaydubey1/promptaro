# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Connections: sign in once, keys stored in the OS keychain. Author: Akshay Dubey

- "Sign in with OpenRouter": standard OAuth PKCE. You log in on openrouter.ai,
  it hands back a key that only you control (revocable from your OpenRouter
  account). One sign-in reaches Claude, GPT, Gemini and others.
- Direct keys (optional) for Anthropic / OpenAI / Google: pasted once into
  the page, checked against the vendor's API, then stored in the keychain.
  Direct connections skip OpenRouter's fee.
- Ollama: detected automatically when it is running.

Key values never leave this machine except to the vendor they belong to, and
no endpoint ever returns them.
"""
from __future__ import annotations

import asyncio
import base64
import re
import hashlib
import secrets as pysecrets
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import catalog, copilot, quota, secrets
from .free_providers import FREE_PROVIDERS
from .free_providers import detect as detect_free

api = APIRouter()

OR_AUTH = "https://openrouter.ai/auth"
OR_EXCHANGE = "https://openrouter.ai/api/v1/auth/keys"

VENDORS = {
    "openrouter": {"key": "OPENROUTER_API_KEY", "label": "OpenRouter",
                   "get_key_url": "https://openrouter.ai/settings/keys"},
    "anthropic": {"key": "ANTHROPIC_API_KEY", "label": "Anthropic (Claude)",
                  "get_key_url": "https://platform.claude.com/settings/keys"},
    "openai": {"key": "OPENAI_API_KEY", "label": "OpenAI",
               "get_key_url": "https://platform.openai.com/api-keys"},
    "google": {"key": "GEMINI_API_KEY", "label": "Google (Gemini)",
               "get_key_url": "https://aistudio.google.com/app/apikey"},
}

for _p in FREE_PROVIDERS.values():                 # free tiers: one key each, checked against the provider
    VENDORS[_p.id] = {"key": _p.key_env, "label": _p.label, "get_key_url": _p.get_key_url, "free": True}

_pending: dict[str, tuple[str, float]] = {}      # nonce -> (code_verifier, expires_at)


def _router(request: Request):
    return request.app.state.router


@api.get("/connect/status")
async def status(request: Request):
    r = _router(request)
    usable = {m.id for m in r.registry.usable()}
    if secrets.has("OPENROUTER_API_KEY"):
        await catalog.FREE.refresh(r._client)
    vendors = sorted({m.extra.get("vendor", "") for m in r.registry.models.values()
                      if m.id in usable and m.provider != "offline"} - {""})
    return {
        "storage": secrets.backend(),
        "free_quota": catalog.FREE.snapshot() if secrets.has("OPENROUTER_API_KEY") else None,
        "families": vendors,
        "connections": {v: {"label": d["label"], "connected": secrets.has(d["key"]), "get_key_url": d["get_key_url"]}
                        for v, d in VENDORS.items() if not d.get("free")},
        "free_providers": [{"id": fp.id, "label": fp.label, "connected": secrets.has(fp.key_env),
                            "plan": fp.plan,
                            "get_key_url": fp.get_key_url, "trains": fp.trains, "limits": fp.limits,
                            "note": fp.note, "checked": fp.checked, "source": fp.source}
                           for fp in FREE_PROVIDERS.values()],
        "free_usage": quota.LEDGER.snapshot(list(r.registry.usable())),
        "ollama": await asyncio.to_thread(catalog.ollama_models),
        "settings": catalog.get_settings(),
        "models": [{"id": m.id, "name": m.extra.get("name", m.id), "via": m.extra.get("via", ""),
                    "free": bool(m.extra.get("free")),
                    "tier": m.tier, "usable": m.id in usable,
                    "price_per_1m": {"input": round(m.price_in, 3), "output": round(m.price_out, 3)}}
                   for m in r.registry.models.values() if not m.fallback_only],
    }


@api.get("/connect/openrouter/start")
async def openrouter_start(request: Request):
    now = time.time()
    for k in [k for k, (_, exp) in _pending.items() if exp < now]:
        _pending.pop(k, None)
    verifier = pysecrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    nonce = pysecrets.token_urlsafe(16)
    _pending[nonce] = (verifier, now + 600)
    cb = str(request.base_url).rstrip("/") + f"/connect/openrouter/callback/{nonce}"
    q = httpx.QueryParams({"callback_url": cb, "code_challenge": challenge, "code_challenge_method": "S256"})
    return RedirectResponse(f"{OR_AUTH}?{q}", status_code=302)


@api.get("/connect/openrouter/callback/{nonce}")
async def openrouter_callback(nonce: str, request: Request, code: str = ""):
    item = _pending.pop(nonce, None)
    if not item or item[1] < time.time():
        return RedirectResponse("/?error=Sign-in+link+expired.+Please+try+again.", 302)
    if not code:
        return RedirectResponse("/?error=Sign-in+was+cancelled.", 302)
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.post(OR_EXCHANGE, json={"code": code, "code_verifier": item[0],
                                                   "code_challenge_method": "S256"})
            resp.raise_for_status()
            key = resp.json().get("key", "")
    except Exception:
        return RedirectResponse("/?error=OpenRouter+did+not+accept+the+sign-in.+Please+try+again.", 302)
    if not key:
        return RedirectResponse("/?error=OpenRouter+returned+no+key.", 302)
    secrets.set("OPENROUTER_API_KEY", key)
    await asyncio.to_thread(_router(request).reload)
    return RedirectResponse("/?connected=openrouter", 302)


class SettingsBody(BaseModel):
    free_models: str | None = Field(None, pattern="^(off|chat|all)$")
    gateway_spend: str | None = Field(None, pattern="^(free|paid_ok)$")
    daily_budget_usd: float | None = Field(None, ge=0, le=10000)


@api.post("/connect/copilot/device")
async def copilot_device_start(request: Request):
    """GitHub device flow: returns a code for the person to enter at github.com/login/device."""
    try:
        return await asyncio.to_thread(copilot.start_device)
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach GitHub to start sign-in: {secrets.redact(str(e))[:200]}")


@api.get("/connect/copilot/device/status")
async def copilot_device_status(request: Request):
    """Poll while the person approves in the browser; connects when GitHub says so."""
    try:
        d = await asyncio.to_thread(copilot.poll_device)
    except Exception as e:
        raise HTTPException(502, f"GitHub sign-in check failed: {secrets.redact(str(e))[:200]}")
    if d["status"] == "connected":
        try:
            await asyncio.to_thread(catalog.discover_free, "github_copilot")
        except Exception as e:
            copilot.disconnect()
            raise HTTPException(502, f"Signed in, but couldn't list Copilot models "
                                     f"(is a Copilot plan active?): {secrets.redact(str(e))[:200]}")
        await asyncio.to_thread(_router(request).reload)
        d["models"] = sum(1 for m in _router(request).registry.usable()
                          if m.extra.get("vendor") == "github_copilot")
    return d


@api.post("/connect/settings")
async def save_settings(body: SettingsBody, request: Request):
    """free_models: off | chat (default) | all.  gateway_spend: free (default) | paid_ok.
    daily_budget_usd: hard daily cap on paid model spend across every surface; 0 = no cap."""
    d = catalog.set_settings(free_models=body.free_models, gateway_spend=body.gateway_spend,
                             daily_budget_usd=body.daily_budget_usd)
    await asyncio.to_thread(_router(request).reload)
    return d


@api.post("/connect/refresh")
async def refresh(request: Request):
    """Re-pull the live model list and prices."""
    p = catalog.HOME / "openrouter_models.json"
    p.unlink(missing_ok=True)
    for v in ("anthropic", "openai", "google"):
        if secrets.has(VENDORS[v]["key"]):
            try:
                await asyncio.to_thread(catalog.discover_native, v)
            except Exception:
                pass
    for pid, fp in FREE_PROVIDERS.items():
        if secrets.has(fp.key_env):
            try:
                await asyncio.to_thread(catalog.discover_free, pid)
            except Exception:
                pass
    await asyncio.to_thread(_router(request).reload)
    return {"ok": True}


class KeyBody(BaseModel):
    key: str = Field(..., min_length=8, max_length=500)
    workspace_id: str | None = Field(None, max_length=100)   # Anthropic organization-scoped keys only


def clean_key(raw: str) -> str:
    """Strip what people often copy along with a key: spaces, quotes, 'Bearer ', 'KEY=' prefixes."""
    k = raw.strip().strip('"').strip("'").strip()
    if "=" in k and k.split("=", 1)[0].isupper():          # e.g. OPENROUTER_API_KEY=sk-or-...
        k = k.split("=", 1)[1].strip().strip('"').strip("'")
    if k.lower().startswith("bearer "):
        k = k[7:].strip()
    return k


def detect_vendor(key: str) -> str | None:
    """Recognise a key by its prefix, so it works whichever box it was pasted in."""
    k = clean_key(key)
    free = detect_free(k)
    if free:
        return free
    if k.startswith("sk-or-"):
        return "openrouter"
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("AIza"):
        return "google"
    if k.startswith(("sk-proj-", "sk-svcacct-", "sk-admin-")) or (k.startswith("sk-") and len(k) > 30):
        return "openai"
    return None


def _provider_reason(e: Exception) -> str:
    """The provider's own words, without ever echoing the key."""
    resp = getattr(e, "response", None)
    if resp is None:
        return f"{type(e).__name__}: {secrets.redact(str(e))[:160]}" if str(e) else type(e).__name__
    try:
        d = resp.json()
        err = d.get("error", d)
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        msg = (resp.text or "")[:200]
    return f"HTTP {resp.status_code}: {secrets.redact(msg)[:200]}"


@api.post("/connect/key")
async def connect_any_key(body: KeyBody, request: Request):
    """Paste any provider's key; the provider is detected from the key itself."""
    vendor = detect_vendor(body.key)
    if not vendor:
        raise HTTPException(400, "Couldn't tell which provider this key is for. Paste it in that provider's row.")
    return await _connect(vendor, body.key, request, body.workspace_id)


@api.post("/connect/{vendor}")
async def connect_key(vendor: str, body: KeyBody, request: Request):
    if vendor not in VENDORS:
        raise HTTPException(404, "unknown provider")
    detected = detect_vendor(body.key)
    return await _connect(detected or vendor, body.key, request, body.workspace_id)


_WS_RE = re.compile(r"^wrkspc_[A-Za-z0-9]{6,64}$")


def _needs_workspace(e: Exception) -> bool:
    resp = getattr(e, "response", None)
    return resp is not None and resp.status_code == 400 and "anthropic-workspace-id" in (resp.text or "")


async def _list_anthropic_workspaces(key: str) -> list[dict]:
    """Best effort: some organization keys may list their workspaces; if not, the user is asked."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://api.anthropic.com/v1/organizations/workspaces",
                            params={"include_default": "true", "limit": 100},
                            headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            if r.status_code != 200:
                return []
            return [{"id": w.get("id"), "name": w.get("name") or w.get("id")}
                    for w in r.json().get("data", []) if not w.get("archived_at")]
    except Exception:
        return []


async def _connect(vendor: str, raw_key: str, request: Request, workspace_id: str | None = None) -> dict:
    name, key = VENDORS[vendor]["key"], clean_key(raw_key)
    label = VENDORS[vendor]["label"]
    previous = secrets.get(name)
    prev_ws = secrets.get("ANTHROPIC_WORKSPACE_ID")
    ws = (workspace_id or "").strip()
    if vendor == "anthropic":
        if ws and not _WS_RE.match(ws):
            raise HTTPException(400, {"vendor": vendor, "code": "needs_workspace", "workspaces": [],
                                      "message": "That doesn't look like a workspace ID. It starts with wrkspc_."})
        secrets.delete("ANTHROPIC_WORKSPACE_ID")          # a new key starts clean
        if ws:
            secrets.set("ANTHROPIC_WORKSPACE_ID", ws)
    secrets.set(name, key)
    try:
        if vendor == "openrouter":
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get("https://openrouter.ai/api/v1/key", headers={"authorization": f"Bearer {key}"})
                r.raise_for_status()
        elif vendor in FREE_PROVIDERS:
            ids = await asyncio.to_thread(catalog.discover_free, vendor)
            if not ids:
                raise ValueError("the key works but no models are visible to it")
        else:
            ids = await asyncio.to_thread(catalog.discover_native, vendor)
            if not ids:
                raise ValueError("the key works but no models are visible to it")
    except Exception as e:
        if previous:
            secrets.set(name, previous)
        else:
            secrets.delete(name)
        if vendor == "anthropic":
            if prev_ws:
                secrets.set("ANTHROPIC_WORKSPACE_ID", prev_ws)
            else:
                secrets.delete("ANTHROPIC_WORKSPACE_ID")
            if _needs_workspace(e) and not ws:
                found = await _list_anthropic_workspaces(key)
                if len(found) == 1:                          # only one choice: use it without asking
                    return await _connect(vendor, key, request, found[0]["id"])
                print("[connect] Anthropic key is organization-scoped; asking which workspace to use", flush=True)
                raise HTTPException(400, {
                    "vendor": vendor, "code": "needs_workspace", "workspaces": found,
                    "message": "This Anthropic key belongs to your whole organization, so Anthropic needs to know "
                               "which workspace to bill. Choose one, or paste its ID (starts with wrkspc_).",
                    "console_url": "https://platform.claude.com/settings/workspaces"})
        reason = _provider_reason(e)
        print(f"[connect] {label} key check failed: {reason}", flush=True)     # visible in the Terminal window
        code = getattr(getattr(e, "response", None), "status_code", None)
        hint = {401: "The key is wrong, revoked, or for a different provider.",
                403: "The key isn't allowed to list models. Check its permissions in the provider console.",
                402: "The account has no credit. Add credit in the provider console.",
                429: "The provider is rate-limiting this key right now. Try again in a minute."}.get(code, "")
        raise HTTPException(400, {"vendor": vendor, "message": f"{label} didn't accept this key. {hint}".strip(),
                                  "detail": reason})
    await asyncio.to_thread(_router(request).reload)
    if vendor == "openrouter":
        await catalog.FREE.refresh(_router(request)._client, force=True)
    usable = [m for m in _router(request).registry.usable() if m.provider != "offline"]
    return {"connected": True, "vendor": vendor, "label": label, "models": len(usable)}


@api.delete("/connect/{vendor}")
async def disconnect(vendor: str, request: Request):
    if vendor == "github_copilot":
        copilot.disconnect()
        await asyncio.to_thread(_router(request).reload)
        return {"connected": False}
    if vendor not in VENDORS:
        raise HTTPException(404, "unknown provider")
    secrets.delete(VENDORS[vendor]["key"])
    if vendor == "anthropic":
        secrets.delete("ANTHROPIC_WORKSPACE_ID")
    await asyncio.to_thread(_router(request).reload)
    return {"connected": False}

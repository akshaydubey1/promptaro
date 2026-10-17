# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""HTTP API. Author: Akshay Dubey

  POST /v1/run       one instruction in, checked result out
  POST /v1/route     routing decision only (no model call) — for the <100ms target
  GET  /v1/registry  full catalog + availability detail
  GET  /v1/stats     learned performance per model / task type / complexity
  GET  /healthz

Auth: set ROUTER_TOKEN to require Bearer auth on everything (needed when binding
beyond loopback). Without it, mutations still require the x-promptaro app header.
"""
from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Literal

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .connect import api as connect_api
from .engine import Router, RunRequest

ROOT = os.environ.get("ROUTER_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
router: Router | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global router
    router = Router(ROOT, db_path=os.environ.get("ROUTER_DB"))
    app.state.router = router
    yield
    await router.aclose()


app = FastAPI(title="Promptaro Smart Router", version="0.2.0", lifespan=lifespan)
# Only answer requests addressed to this machine (blocks DNS-rebinding tricks
# that could let a website spend your credits through the local server).
_hosts = os.environ.get("ROUTER_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in _hosts if h.strip()])

# These paths authenticate themselves (gateway bearer token) or must be reachable
# from a plain browser redirect (OAuth start/callback).
_SELF_AUTH = ("/v1/messages", "/v1/chat/completions", "/v1/models")
_OPEN_GET = ("/", "/healthz", "/connect/openrouter/start", "/connect/openrouter/callback")


def _has_token_auth(request: Request) -> bool:
    supplied = request.headers.get("x-api-key") or ""
    auth = request.headers.get("authorization") or ""
    if not supplied and auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    token = os.environ.get("ROUTER_TOKEN", "")
    return bool(token) and bool(supplied) and hmac.compare_digest(supplied.encode(), token.encode())


@app.middleware("http")
async def _guard(request: Request, call_next):
    """Local API guard. With ROUTER_TOKEN set (required for non-loopback binds), every
    endpoint needs Bearer/x-api-key auth. Without it, mutating requests still need the
    app header, which browsers can't set cross-origin — blocking CSRF spend attempts."""
    path = request.url.path
    if any(path.startswith(p) for p in _SELF_AUTH):
        return await call_next(request)
    if os.environ.get("ROUTER_TOKEN"):
        if _has_token_auth(request):
            return await call_next(request)
        if request.method in ("GET", "HEAD", "OPTIONS") and any(path.startswith(p) for p in _OPEN_GET):
            return await call_next(request)
        return JSONResponse({"detail": "Unauthorized — send Authorization: Bearer <ROUTER_TOKEN>."},
                            status_code=401)
    if request.method not in ("GET", "HEAD", "OPTIONS") and \
            request.headers.get("x-promptaro") != "1" and request.headers.get("x-tillwright") != "1":
        return JSONResponse({"detail": "Missing the app header (x-promptaro: 1)."}, status_code=403)
    return await call_next(request)


app.include_router(connect_api)
_UI = Path(__file__).with_name("ui.html")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui():
    return _UI.read_text()


class RunBody(BaseModel):
    instruction: str = Field(..., min_length=1)
    context: str = ""
    has_images: bool = False
    tests: str | None = None
    prefer: Literal["balanced", "cheap", "fast", "best"] = "balanced"
    quality_target: float | None = Field(None, ge=0, le=1)
    max_cost_usd: float | None = Field(None, gt=0)
    force_model: str | None = None
    use_cache: bool = True
    cross_review: Literal["off", "auto", "always"] | None = None
    private: bool = False                 # skip free providers that may train on prompts
    allow_free: bool | None = None        # None = follow the "free models" setting
    system_extra: str = ""
    exclude: tuple[str, ...] = ()


@app.post("/v1/run")
async def run(body: RunBody):
    try:
        res = await router.run(RunRequest(**body.model_dump()))
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    return res.as_dict()


@app.post("/v1/route")
async def route_only(body: RunBody):
    try:
        res = await router.run(RunRequest(**body.model_dump(), dry_run=True))
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    d = res.as_dict()
    d.pop("output", None)
    d.pop("attempts", None)
    return d


@app.get("/v1/registry")
async def registry_models():
    """Full model list with availability detail (the gateway's /v1/models serves tools)."""
    usable = {m.id for m in router.registry.usable()}
    return [{"id": m.id, "provider": m.provider, "model": m.model, "tier": m.tier,
             "enabled": m.enabled, "has_key": m.has_key(), "usable": m.id in usable,
             "capabilities": sorted(m.capabilities),
             "price_per_1m": {"input": m.price_in, "output": m.price_out}}
            for m in router.registry.models.values()]


@app.get("/v1/stats")
async def stats():
    return router.store.stats()


@app.get("/healthz")
async def health():
    return {"ok": True, "usable_models": [m.id for m in router.registry.usable()]}


# Coding-tools gateway (Anthropic Messages + OpenAI Chat Completions). Its /v1/models
# serves the OpenAI/Anthropic-shaped list tools expect; the full registry is at /v1/registry.
from .gateway import api as gateway_api  # noqa: E402

app.include_router(gateway_api)

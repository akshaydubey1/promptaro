# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Live model catalog. Author: Akshay Dubey

Nothing to fill in by hand: model names, prices, context sizes and
capabilities come from OpenRouter's public model list (no key needed to
read it). For each vendor we keep a small, medium and large current model.

Each chosen model is then wired to the best connection you have:
  direct key for that vendor (no middleman fee)  >  OpenRouter sign-in  >  skipped
Native model ids for direct connections are discovered from the vendor's own
/models endpoint, so nothing is guessed.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx

from . import migrate, secrets
from .free_providers import FAMILY_TRUST, FREE_PROVIDERS, limits_for, pick_models

HOME = migrate.home()                        # ~/.promptaro, migrating ~/.akshay-router & ~/.tillwright
OR_MODELS_URL = "https://openrouter.ai/api/v1/models"
OR_BASE = "https://openrouter.ai/api/v1"

# vendor -> how to reach it directly
DIRECT = {
    "anthropic": {"provider": "anthropic", "key": "ANTHROPIC_API_KEY", "base_url": ""},
    "openai": {"provider": "openai", "key": "OPENAI_API_KEY", "base_url": ""},
    "google": {"provider": "openai_compatible", "key": "GEMINI_API_KEY",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
}
_SKIP = re.compile(r"(audio|image-|realtime|search|tts|embed|guard|moderation|transcribe|-instruct$|vision-preview)", re.I)
# How much to trust each model family before Promptaro has its own results.
# Unknown families start lower, so a cheap unknown model isn't chosen on price
# alone; good results (checks passed, thumbs up) raise them over time.
REPUTATION = {
    "anthropic": 0.0, "openai": 0.0, "google": 0.0,
    "deepseek": -0.02, "x-ai": -0.02, "qwen": -0.03, "moonshotai": -0.03, "z-ai": -0.03,
    "mistralai": -0.04, "meta-llama": -0.05, "minimax": -0.05, "cohere": -0.06, "amazon": -0.06,
    "microsoft": -0.06, "nvidia": -0.06, "perplexity": -0.05, "baidu": -0.06, "ai21": -0.07,
}
UNKNOWN_REPUTATION = -0.09
_NOT_VENDORS = {"openrouter"}            # router pseudo-models, not model families
MAX_VENDORS = 30


def price_tier(m: dict) -> int:
    """Rough size class from price per 1M tokens (average of input and output)."""
    try:
        avg = (float(m["pricing"]["prompt"]) + float(m["pricing"]["completion"])) * 1e6 / 2
    except (KeyError, TypeError, ValueError):
        return 2
    return 1 if avg < 1.5 else (2 if avg < 10 else 3)


def all_vendors(models: list[dict], max_vendors: int = MAX_VENDORS, recent_days: int = 365) -> list[str]:
    """Every model family on the market with a current paid text model: known ones first, then by size."""
    newest = max((m.get("created", 0) for m in models), default=0)
    count: dict[str, int] = {}
    for m in models:
        v = m.get("id", "").split("/", 1)[0]
        if v in _NOT_VENDORS or not _usable_text_model(m) or m.get("created", 0) < newest - recent_days * 86400:
            continue
        count[v] = count.get(v, 0) + 1
    order = sorted(count, key=lambda v: (v not in REPUTATION, REPUTATION.get(v, 0) * -1, -count[v], v))
    return order[:max_vendors]


TIER_PRIOR = {
    1: {"coding": 0.72, "research": 0.66, "writing": 0.74, "document": 0.70, "data": 0.69, "chat": 0.80},
    2: {"coding": 0.87, "research": 0.83, "writing": 0.85, "document": 0.84, "data": 0.84, "chat": 0.86},
    3: {"coding": 0.93, "research": 0.90, "writing": 0.89, "document": 0.89, "data": 0.90, "chat": 0.88},
}


def _cache(name: str) -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME / name


def _read_cache(name: str, max_age_s: float) -> dict | list | None:
    p = _cache(name)
    try:
        if time.time() - p.stat().st_mtime <= max_age_s:
            return json.loads(p.read_text())
    except Exception:
        pass
    return None


def _write_cache(name: str, data) -> None:
    _cache(name).write_text(json.dumps(data))


def fetch_openrouter_models(max_age_s: float = 86400, client: httpx.Client | None = None) -> list[dict]:
    hit = _read_cache("openrouter_models.json", max_age_s)
    if hit is not None:
        return hit
    try:
        c = client or httpx.Client(timeout=15)
        r = c.get(OR_MODELS_URL)
        r.raise_for_status()
        data = r.json().get("data", [])
        _write_cache("openrouter_models.json", data)
        return data
    except Exception:
        stale = _read_cache("openrouter_models.json", 10 ** 9)   # any age beats nothing
        return stale or []


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def discover_native(vendor: str, client: httpx.Client | None = None) -> list[str]:
    """List model ids the vendor's own API accepts with your key. Also validates the key."""
    key = secrets.get(DIRECT[vendor]["key"])
    if not key:
        return []
    c = client or httpx.Client(timeout=15)
    if vendor == "anthropic":
        r = c.get("https://api.anthropic.com/v1/models?limit=1000",
                  headers=secrets.anthropic_headers(key))
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", [])]
    elif vendor == "openai":
        r = c.get("https://api.openai.com/v1/models", headers={"authorization": f"Bearer {key}"})
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", [])]
    elif vendor == "google":
        r = c.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key, "pageSize": 1000})
        r.raise_for_status()
        ids = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])]
    else:
        return []
    _write_cache(f"native_{vendor}.json", ids)
    return ids


def _native_id(vendor: str, or_id: str) -> str | None:
    ids = _read_cache(f"native_{vendor}.json", 10 ** 9) or []
    want = _norm(or_id.split("/", 1)[1])
    exact = [i for i in ids if _norm(i) == want]
    if exact:
        return exact[0]
    # dated snapshots, e.g. claude-haiku-4-5 -> claude-haiku-4-5-20251001 (pick newest)
    pref = sorted((i for i in ids if _norm(i).startswith(want + "-") and re.fullmatch(r"\d{6,8}", _norm(i)[len(want) + 1:])),
                  reverse=True)
    return pref[0] if pref else None


def discover_free(pid: str, client: httpx.Client | None = None) -> list[str]:
    """List a free provider's models with the user's key. Also validates the key."""
    p = FREE_PROVIDERS[pid]
    key = secrets.get(p.key_env)
    if not key:
        return []
    c = client or httpx.Client(timeout=15)
    r = c.get(f"{p.base_url}/models", headers={"authorization": f"Bearer {key}"})
    r.raise_for_status()
    d = r.json()
    rows = d.get("data", d.get("models", [])) if isinstance(d, dict) else d
    ids = [m.get("id") or m.get("name") for m in rows if isinstance(m, dict)]
    ids = [i for i in ids if i]
    _write_cache(f"free_{pid}.json", ids)
    return ids


def free_provider_specs() -> list[dict]:
    """Model entries for every free provider the user has a key for."""
    specs = []
    for pid, p in FREE_PROVIDERS.items():
        if not secrets.has(p.key_env):
            continue
        ids = _read_cache(f"free_{pid}.json", 10 ** 9) or list(p.fallback_models)
        for model, rule in pick_models(p, ids):
            caps = {"json"}
            if rule.tools:
                caps.add("tools")
            if rule.vision:
                caps.add("vision")
            if rule.context >= 150_000:
                caps.add("long_context")
            trust = FAMILY_TRUST.get(rule.family, -0.08)
            specs.append({
                "id": f"{pid}/{model}", "name": f"{model.split('/')[-1]} ({p.label})",
                "provider": "openai_compatible",
                "model": model, "api_key_env": p.key_env, "base_url": p.base_url,
                "via": f"{p.label} (free)",
                "free": True, "trains": p.trains, "quota": pid, "limits": limits_for(p, rule), "vendor": pid,
                "tier": rule.tier, "context_window": rule.context, "capabilities": sorted(caps),
                "price": {"input": 0.0, "output": 0.0}, "latency_ms_p50": p.latency_ms,
                "prior": {k: max(0.0, min(1.0, v + trust)) for k, v in TIER_PRIOR[rule.tier].items()},
            })
    return specs


# ------------------------------------------------------------ settings
FREE_MODES = ("off", "chat", "all")     # where free models may be used


def get_settings() -> dict:
    d = _read_cache("settings.json", 10 ** 12) or {}
    # off  : never use free models
    # chat : chats may use any free model; code (agents, coding tools) only free providers that
    #        don't train on prompts  (default)
    # all  : code may also use free providers that may train on prompts
    d.setdefault("free_models", "chat")
    d.setdefault("gateway_spend", "free")      # coding tools: "free" = never spend money, "paid_ok" = may
    d.setdefault("daily_budget_usd", 0.0)      # hard daily cap on paid spend; 0 = no cap
    return d


def set_settings(**kw) -> dict:
    d = get_settings()
    d.update({k: v for k, v in kw.items() if v is not None})
    _write_cache("settings.json", d)
    return d


def _is_free(m: dict) -> bool:
    p = m.get("pricing") or {}
    try:
        return str(m.get("id", "")).endswith(":free") and float(p.get("prompt", 1)) == 0 \
            and float(p.get("completion", 1)) == 0
    except (TypeError, ValueError):
        return False


def choose_free(models: list[dict], limit: int = 4, recent_days: int = 365) -> list[dict]:
    """Newest free text models that can follow structured instructions (JSON/tools)."""
    free, plain = [], []
    for m in models:
        if not _is_free(m) or _SKIP.search(m["id"]):
            continue
        arch = m.get("architecture") or {}
        if "text" not in (arch.get("output_modalities") or ["text"]):
            continue
        if int(m.get("context_length") or 0) < 16000:
            continue
        params = set(m.get("supported_parameters") or [])
        (free if params & {"tools", "response_format", "structured_outputs"} else plain).append(m)
    free = free or plain          # fine for chat even without structured-output support
    if not free:
        return []
    newest = max(m.get("created", 0) for m in free)
    free = [m for m in free if m.get("created", 0) >= newest - recent_days * 86400]
    free.sort(key=lambda m: (m.get("created", 0), m.get("context_length", 0)), reverse=True)
    return free[:limit]


def _usable_text_model(m: dict) -> bool:
    mid = m.get("id", "")
    if ":" in mid or _SKIP.search(mid):
        return False
    arch = m.get("architecture") or {}
    if "text" not in (arch.get("output_modalities") or ["text"]):
        return False
    p = m.get("pricing") or {}
    try:
        return float(p.get("prompt", -1)) > 0 and float(p.get("completion", -1)) > 0
    except (TypeError, ValueError):
        return False


def choose(models: list[dict], vendors: list[str], per_vendor_recent: int = 8,
           recent_days: int = 400) -> list[tuple[str, int, dict]]:
    """Pick (vendor, tier, model) — cheapest, middle and priciest among each vendor's recent models."""
    out = []
    for v in vendors:
        vm = [m for m in models if m.get("id", "").startswith(v + "/") and _usable_text_model(m)]
        if not vm:
            continue
        newest = max(m.get("created", 0) for m in vm)
        vm = [m for m in vm if m.get("created", 0) >= newest - recent_days * 86400]
        vm = sorted(vm, key=lambda m: m.get("created", 0), reverse=True)[:per_vendor_recent]
        by_price = sorted(vm, key=lambda m: float(m["pricing"]["prompt"]) + float(m["pricing"]["completion"]))
        if len(by_price) == 1:
            picks = {2: by_price[0]}
        elif len(by_price) == 2:
            picks = {1: by_price[0], 3: by_price[1]}
        else:
            picks = {1: by_price[0], 2: by_price[len(by_price) // 2], 3: by_price[-1]}
        seen = set()
        for tier, m in picks.items():
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            out.append((v, tier, m))
    return out


OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


def ollama_models(client: httpx.Client | None = None) -> list[str]:
    try:
        c = client or httpx.Client(timeout=1.5)
        r = c.get(f"{OLLAMA}/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", []) if "embed" not in m["name"]]
    except Exception:
        return []


def build_specs(cfg: dict, client: httpx.Client | None = None, cached_only: bool = False) -> list[dict]:
    """Return model dicts in the same shape as config/models.yaml entries.

    cached_only=True serves whatever is already on disk and never touches the
    network — used at startup so the app can open immediately, with the real
    refresh happening in the background afterwards."""
    if not cfg.get("enabled", True):
        return []
    local = []
    if cfg.get("ollama", "auto") == "auto":
        for name in ollama_models()[:3]:
            local.append({"id": f"ollama/{name}", "name": f"{name} (local)", "provider": "openai_compatible",
                          "model": name, "base_url": f"{OLLAMA}/v1", "api_key_env": "", "via": "local",
                          "tier": 1, "context_window": 32000, "capabilities": ["json"],
                          "price": {"input": 0.0, "output": 0.0}, "latency_ms_p50": 2500,
                          "prior": {k: v - 0.15 for k, v in TIER_PRIOR[1].items()}})
    free_mode = get_settings().get("free_models", "chat")
    if cached_only:
        models = _read_cache("openrouter_models.json", 10 ** 9) or []
    else:
        models = fetch_openrouter_models(cfg.get("refresh_s", 86400), client)
    vendors = cfg.get("vendors", "all")
    if vendors == "all" or vendors == ["all"]:
        vendors = all_vendors(models, int(cfg.get("max_vendors", MAX_VENDORS)))
    has_or = secrets.has("OPENROUTER_API_KEY")
    specs = []
    for vendor, tier, m in choose(models, vendors, cfg.get("per_vendor_recent", 8)):
        d = DIRECT.get(vendor)
        native = _native_id(vendor, m["id"]) if d and secrets.has(d["key"]) else None
        if native:
            conn = {"provider": d["provider"], "model": native, "api_key_env": d["key"], "base_url": d["base_url"],
                    "via": "direct"}
        elif has_or:
            conn = {"provider": "openai_compatible", "model": m["id"], "api_key_env": "OPENROUTER_API_KEY",
                    "base_url": OR_BASE, "via": "openrouter"}
        else:
            continue
        arch = m.get("architecture") or {}
        params = set(m.get("supported_parameters") or [])
        caps = {"json"} if ("response_format" in params or "structured_outputs" in params) else set()
        if "tools" in params:
            caps.add("tools")
        if "image" in (arch.get("input_modalities") or []):
            caps.add("vision")
        ctx = int(m.get("context_length") or 32000)
        if ctx >= 150_000:
            caps.add("long_context")
        if conn["provider"] == "anthropic":
            caps.add("web")
        tier = min(tier, price_tier(m))       # a cheap "largest" model is still a small one
        prior = dict(TIER_PRIOR[tier])
        bias = float(cfg.get("vendor_bias", {}).get(vendor, REPUTATION.get(vendor, UNKNOWN_REPUTATION)))
        prior = {k: max(0.0, min(1.0, x + bias)) for k, x in prior.items()}
        specs.append({
            "id": m["id"], "name": m.get("name", m["id"]), **conn, "tier": tier, "context_window": ctx,
            "vendor": vendor,
            "capabilities": sorted(caps),
            "price": {"input": float(m["pricing"]["prompt"]) * 1e6, "output": float(m["pricing"]["completion"]) * 1e6},
            "latency_ms_p50": {1: 900, 2: 1800, 3: 3200}[tier], "prior": prior,
        })
    # Free models (OpenRouter ":free" endpoints): $0, rate-limited, and some
    # providers may log or train on prompts — so by default only chats use them.
    if has_or and free_mode != "off":
        for m in choose_free(models):
            arch = m.get("architecture") or {}
            params = set(m.get("supported_parameters") or [])
            caps = {"json"} if params & {"response_format", "structured_outputs"} else set()
            if "tools" in params:
                caps.add("tools")
            if "image" in (arch.get("input_modalities") or []):
                caps.add("vision")
            ctx = int(m.get("context_length") or 32000)
            if ctx >= 150_000:
                caps.add("long_context")
            specs.append({
                "id": m["id"], "name": m.get("name", m["id"]), "provider": "openai_compatible", "model": m["id"],
                "api_key_env": "OPENROUTER_API_KEY", "base_url": OR_BASE, "via": "openrouter (free)", "free": True,
                "trains": True,               # some free endpoints log or train on prompts
                "tier": 1, "context_window": ctx, "capabilities": sorted(caps),
                "price": {"input": 0.0, "output": 0.0}, "latency_ms_p50": 2500,
                # Free endpoints are slower and rate-limited; start them a little below paid small models.
                "prior": {k: v - 0.06 for k, v in TIER_PRIOR[1].items()},
            })
    return specs + free_provider_specs() + local


# ------------------------------------------------------------ free quota
OR_KEY_URL = "https://openrouter.ai/api/v1/key"


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


class FreeQuota:
    """How many free-model requests are left today (OpenRouter's daily allowance).

    OpenRouter allows 50 free requests a day, or 1000 once $10 of credit has ever
    been bought, plus 20 a minute. We read the allowance from OpenRouter, count
    our own use between checks, and pause free models after a "rate limited"
    reply, so routing never waits on a free model that will refuse.
    """

    REFRESH_S = 300

    def __init__(self):
        self.limit: int | None = None
        self.remaining: int | None = None
        self.checked = 0.0
        self.cooldown_until = 0.0
        self.reason = ""
        self._load()

    # local count survives restarts, resets each UTC day
    def _load(self) -> None:
        d = _read_cache("free_usage.json", 10 ** 12) if HOME.exists() else None
        self.day, self.used = (d["day"], int(d["used"])) if d and d.get("day") == _utc_day() else (_utc_day(), 0)

    def _save(self) -> None:
        try:
            _write_cache("free_usage.json", {"day": self.day, "used": self.used})
        except OSError:
            pass

    def _roll(self) -> None:
        if self.day != _utc_day():
            self.day, self.used = _utc_day(), 0
            if self.limit is not None:
                self.remaining = self.limit
            if self.reason == "daily":
                self.cooldown_until, self.reason = 0.0, ""

    def available(self) -> bool:
        self._roll()
        return time.time() >= self.cooldown_until and (self.remaining is None or self.remaining > 0)

    def note_used(self) -> None:
        self._roll()
        self.used += 1
        if self.remaining is not None:
            self.remaining = max(0, self.remaining - 1)
        self._save()

    def note_rate_limited(self, message: str = "") -> None:
        """A free model said 429. Daily allowance gone -> wait for tomorrow (UTC); else a minute."""
        msg = message.lower()
        if "day" in msg or "daily" in msg or (self.remaining is not None and self.remaining <= 1):
            t = time.gmtime()
            self.cooldown_until = time.time() + (86400 - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec))
            self.remaining, self.reason = 0, "daily"
        else:
            self.cooldown_until, self.reason = time.time() + 65, "minute"

    def apply(self, data: dict) -> None:
        """Read OpenRouter's /key response."""
        d = data.get("data", data) or {}
        f = d.get("free_model_daily_requests") or {}
        limit = f.get("limit")
        if limit is None:
            limit = 50 if d.get("is_free_tier", True) else 1000
        used = f.get("used")
        used = max(int(used), self.used) if used is not None else self.used
        rem = f.get("remaining")
        self.limit = int(limit)
        self.remaining = max(0, min(int(rem) if rem is not None else self.limit - used, self.limit - used))
        self.checked = time.time()

    async def refresh(self, client, force: bool = False) -> None:
        key = secrets.get("OPENROUTER_API_KEY")
        if not key or (not force and time.time() - self.checked < self.REFRESH_S):
            return
        self.checked = time.time()                      # don't stampede if the call fails
        try:
            r = await client.get(OR_KEY_URL, headers={"authorization": f"Bearer {key}"}, timeout=10)
            if r.status_code == 200:
                self.apply(r.json())
        except Exception:
            pass

    def snapshot(self) -> dict:
        self._roll()
        wait = max(0, int(self.cooldown_until - time.time()))
        return {"limit": self.limit, "remaining": self.remaining, "used_today": self.used,
                "available": self.available(), "paused_for_s": wait, "pause_reason": self.reason if wait else ""}


FREE = FreeQuota()

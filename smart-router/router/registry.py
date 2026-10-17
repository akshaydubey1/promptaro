# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Model catalog + availability tracking. Author: Akshay Dubey"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import secrets


@dataclass
class ModelSpec:
    id: str
    provider: str
    model: str
    tier: int
    context_window: int
    capabilities: set[str]
    price_in: float           # USD / 1M tokens
    price_out: float
    latency_ms_p50: float
    prior: dict[str, float]
    api_key_env: str = ""
    base_url: str = ""
    enabled: bool = True
    fallback_only: bool = False
    extra: dict = field(default_factory=dict)

    def has_key(self) -> bool:
        if self.provider == "offline" or not self.api_key_env:
            return True
        return secrets.has(self.api_key_env)

    def cost(self, tokens_in: int, tokens_out: int, cached_in: int = 0) -> float:
        """cached_in: prompt tokens served from the provider's cache, billed at a discount
        (Anthropic cache reads ~0.1x input price; OpenAI-compatible ~0.5x)."""
        cached_in = min(cached_in, tokens_in)
        mult = 0.1 if self.provider == "anthropic" else 0.5
        return (tokens_in - cached_in + cached_in * mult) / 1e6 * self.price_in \
            + tokens_out / 1e6 * self.price_out


class CircuitBreaker:
    """Marks a model unavailable for a cooldown after repeated failures."""

    def __init__(self, threshold: int = 3, cooldown_s: float = 60.0):
        self.threshold, self.cooldown_s = threshold, cooldown_s
        self._fails: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def available(self, model_id: str) -> bool:
        return time.monotonic() >= self._open_until.get(model_id, 0.0)

    def record(self, model_id: str, ok: bool) -> None:
        with self._lock:
            if ok:
                self._fails[model_id] = 0
                return
            n = self._fails.get(model_id, 0) + 1
            self._fails[model_id] = n
            if n >= self.threshold:
                self._open_until[model_id] = time.monotonic() + self.cooldown_s
                self._fails[model_id] = 0


class Registry:
    def __init__(self, path: str | Path, use_catalog: bool = True, refresh_catalog: bool = True):
        raw = yaml.safe_load(Path(path).read_text())
        self.defaults = raw.get("defaults", {})
        self.quality_targets: dict[str, float] = raw.get("quality_targets", {})
        self.weights: dict[str, float] = raw.get("weights", {})
        self.routing: dict = raw.get("routing", {})
        self.catalog_cfg: dict = raw.get("catalog", {"enabled": False})
        self.models: dict[str, ModelSpec] = {}
        entries = list(raw.get("models", []))
        if use_catalog:
            if self.catalog_cfg.get("enabled", False):
                from .catalog import build_specs
                entries = build_specs(self.catalog_cfg, cached_only=not refresh_catalog) + entries
            else:
                from .catalog import custom_specs        # user endpoints aren't part of the catalog toggle
                entries = custom_specs() + entries
        for m in entries:
            self._add(m)
        self.breaker = CircuitBreaker()

    def _add(self, m: dict) -> None:
        spec = ModelSpec(
            id=m["id"], provider=m["provider"], model=m["model"], tier=int(m.get("tier", 2)),
            context_window=int(m.get("context_window", 128000)),
            capabilities=set(m.get("capabilities", [])),
            price_in=float(m.get("price", {}).get("input", 0)),
            price_out=float(m.get("price", {}).get("output", 0)),
            latency_ms_p50=float(m.get("latency_ms_p50", 2000)),
            prior=dict(m.get("prior", {})),
            api_key_env=m.get("api_key_env", "") or "",
            base_url=m.get("base_url", "") or "",
            enabled=bool(m.get("enabled", True)),
            fallback_only=bool(m.get("fallback_only", False)),
            extra={"via": m.get("via", "config"), "name": m.get("name", m["id"]), "free": bool(m.get("free", False)),
                   "vendor": m.get("vendor") or m["id"].split("/", 1)[0], "trains": bool(m.get("trains", False)),
                   "cost_note": m.get("cost_note", ""),
                   "quota": m.get("quota"), "limits": m.get("limits")},
        )
        self.models.setdefault(spec.id, spec)

    def usable(self) -> list[ModelSpec]:
        """Enabled, keyed, not circuit-broken. Fallback-only models only if nothing else is."""
        real = [m for m in self.models.values()
                if m.enabled and not m.fallback_only and m.has_key() and self.breaker.available(m.id)]
        if real:
            return real
        return [m for m in self.models.values() if m.fallback_only and m.enabled]

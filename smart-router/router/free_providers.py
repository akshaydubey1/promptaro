# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Providers with a real free tier, reached with the user's own key. Author: Akshay Dubey

Each entry says where the provider lives, how to recognise its key, what its
free tier allows, and whether it may train on prompts. Limits change; every
entry carries the date it was checked and the page it came from, and live
rate-limit headers override these numbers whenever a provider sends them.

Policy (deliberate, not an oversight):
  * One key per provider per person, used as that provider intends. No key
    pooling, no multiple accounts, no reselling, no shared keys.
  * No consumer-app logins (Claude.ai, ChatGPT web, Gemini app). Official API
    keys or official CLIs only.
  * Providers that may train on prompts are labelled and are not used for code
    unless the person allows it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelRule:
    pattern: str                    # regex matched against the provider's model id
    tier: int                       # 1 small, 2 medium, 3 large
    family: str                     # for trust and display: openai-oss, qwen, llama, ...
    context: int                    # usable context on the free tier
    tools: bool = True
    vision: bool = False
    limits: dict = field(default_factory=dict)   # per-model overrides of the provider limits


@dataclass(frozen=True)
class FreeProvider:
    id: str
    label: str
    base_url: str
    key_env: str
    get_key_url: str
    key_pattern: str | None         # regex that recognises this provider's keys, if they have a prefix
    limits: dict                    # rpm / rpd / tpm / tpd / rpmonth
    scope: str                      # "model": limits apply per model; "account": shared by all models
    trains: bool                    # may use prompts for training on the free tier
    latency_ms: int
    rules: tuple[ModelRule, ...]
    fallback_models: tuple[str, ...]
    header_requests: str = "minute"   # what x-ratelimit-*-requests counts: "minute" or "day"
    header_tokens: str = "minute"
    note: str = ""
    checked: str = "2026-10-15"
    source: str = ""
    max_models: int = 4


FREE_PROVIDERS: dict[str, FreeProvider] = {p.id: p for p in (
    FreeProvider(
        id="groq", label="Groq", base_url="https://api.groq.com/openai/v1", key_env="GROQ_API_KEY",
        get_key_url="https://console.groq.com/keys", key_pattern=r"^gsk_[A-Za-z0-9]{20,}$",
        limits={"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200_000}, scope="model", trains=False, latency_ms=450,
        header_requests="day", header_tokens="minute",
        rules=(
            ModelRule(r"^openai/gpt-oss-120b$", 2, "openai-oss", 131_072),
            ModelRule(r"^qwen/qwen3[.\d]*-\d+b$", 2, "qwen", 131_072),
            ModelRule(r"^openai/gpt-oss-20b$", 1, "openai-oss", 131_072),
            ModelRule(r"^llama-3\.3-70b", 1, "llama", 131_072, limits={"tpm": 12_000, "tpd": 100_000}),
            ModelRule(r"^llama-3\.1-8b-instant$", 1, "llama", 131_072, limits={"rpd": 14_400, "tpm": 6000, "tpd": 500_000}),
        ),
        fallback_models=("openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"),
        note="Very fast. Small per-minute token allowance, so it suits short requests best.",
        source="https://console.groq.com/docs/rate-limits"),
    FreeProvider(
        id="cerebras", label="Cerebras", base_url="https://api.cerebras.ai/v1", key_env="CEREBRAS_API_KEY",
        get_key_url="https://cloud.cerebras.ai/platform", key_pattern=r"^csk-[A-Za-z0-9]{20,}$",
        limits={"rpm": 5, "tpm": 30_000, "tpd": 1_000_000}, scope="model", trains=False, latency_ms=350,
        rules=(
            ModelRule(r"^gpt-oss-120b$", 2, "openai-oss", 65_536),
            ModelRule(r"^qwen-3[.\d]*-\d+b", 2, "qwen", 65_536, vision=True),
            ModelRule(r"^zai-glm", 2, "glm", 65_536),
            ModelRule(r"^gemma-4", 1, "gemma", 65_536),
        ),
        fallback_models=("gpt-oss-120b",),
        note="The fastest inference available. 1 million free tokens a day per model.",
        source="https://inference-docs.cerebras.ai/support/rate-limits"),
    FreeProvider(
        id="nvidia", label="NVIDIA NIM", base_url="https://integrate.api.nvidia.com/v1", key_env="NVIDIA_API_KEY",
        get_key_url="https://build.nvidia.com/settings/api-keys", key_pattern=r"^nvapi-[A-Za-z0-9_\-]{20,}$",
        limits={"rpm": 40}, scope="account", trains=False, latency_ms=2500,
        rules=(
            ModelRule(r"qwen3?[.\d]*-coder", 3, "qwen", 131_072),
            ModelRule(r"kimi-k2", 3, "kimi", 131_072),
            ModelRule(r"deepseek-v[34]", 3, "deepseek", 131_072),
            ModelRule(r"nemotron-3-super", 2, "nemotron", 131_072),
            ModelRule(r"^openai/gpt-oss-120b$", 2, "openai-oss", 131_072),
            ModelRule(r"llama-3\.3-70b", 1, "llama", 131_072),
        ),
        fallback_models=("nvidia/nemotron-3-super-120b-a12b", "openai/gpt-oss-120b"),
        note="Large open models (Qwen Coder, Kimi, DeepSeek). 40 requests a minute shared by all models; "
             "meant for development and prototyping. Needs phone verification.",
        source="https://build.nvidia.com", max_models=5),
    FreeProvider(
        id="mistral", label="Mistral", base_url="https://api.mistral.ai/v1", key_env="MISTRAL_API_KEY",
        get_key_url="https://console.mistral.ai/api-keys", key_pattern=None,
        limits={"rpm": 60}, scope="account", trains=True, latency_ms=1500,
        rules=(
            ModelRule(r"^devstral-medium-latest$", 3, "mistral", 131_072),
            ModelRule(r"^mistral-medium-latest$", 2, "mistral", 131_072, vision=True),
            ModelRule(r"^devstral-small-latest$", 2, "mistral", 131_072),
            ModelRule(r"^mistral-small-latest$", 1, "mistral", 131_072, vision=True),
        ),
        fallback_models=("mistral-small-latest", "devstral-small-latest"),
        note="Free Experiment plan requires allowing Mistral to train on your prompts. Phone verification.",
        source="https://docs.mistral.ai/deployment/laplateforme/tier/"),
    FreeProvider(
        id="codestral", label="Codestral (Mistral)", base_url="https://codestral.mistral.ai/v1",
        key_env="CODESTRAL_API_KEY", get_key_url="https://console.mistral.ai/codestral", key_pattern=None,
        limits={"rpm": 30, "rpd": 2000}, scope="account", trains=True, latency_ms=1200,
        rules=(ModelRule(r"^codestral-latest$", 2, "mistral", 131_072),),
        fallback_models=("codestral-latest",),
        note="Coding model with its own free key. Treated as possibly training on prompts.",
        source="https://docs.mistral.ai/capabilities/code_generation/"),
    FreeProvider(
        id="cohere", label="Cohere", base_url="https://api.cohere.ai/compatibility/v1", key_env="COHERE_API_KEY",
        get_key_url="https://dashboard.cohere.com/api-keys", key_pattern=None,
        limits={"rpm": 20, "rpmonth": 1000}, scope="account", trains=False, latency_ms=1800,
        rules=(
            ModelRule(r"^command-a-reasoning", 2, "cohere", 131_072),
            ModelRule(r"^command-a-0", 2, "cohere", 131_072),
            ModelRule(r"^command-r7b", 1, "cohere", 131_072),
        ),
        fallback_models=("command-a-03-2025",),
        note="Trial key: 1000 requests a month, non-commercial use only.",
        source="https://docs.cohere.com/docs/rate-limits"),
    FreeProvider(
        id="gemini_free", label="Gemini (free tier)", base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY", get_key_url="https://aistudio.google.com/app/apikey",
        key_pattern=r"^AIza[A-Za-z0-9_\-]{20,}$",
        limits={"rpm": 10, "rpd": 250, "tpm": 250_000}, scope="model", trains=True, latency_ms=1500,
        rules=(
            ModelRule(r"^gemini-[\d.]+-flash$|^gemini-flash-latest$", 2, "gemini", 200_000, vision=True),
            ModelRule(r"^gemini-[\d.]+-flash-lite|^gemini-flash-lite", 1, "gemini", 200_000, vision=True,
                      limits={"rpm": 15, "rpd": 1000}),
        ),
        fallback_models=("gemini-2.5-flash", "gemini-2.5-flash-lite"),
        note="AI Studio's free tier. Free-tier prompts may be used to improve Google products, so "
             "private code uses this only when 'all' free models are allowed.",
        source="https://ai.google.dev/gemini-api/docs/rate-limits"),
)}

# Starting trust per model family (added to the size class's prior). Families with no
# record start lower; real results move them.
FAMILY_TRUST = {"openai-oss": -0.03, "qwen": -0.03, "kimi": -0.02, "deepseek": -0.02, "glm": -0.03,
                "nemotron": -0.04, "mistral": -0.04, "llama": -0.06, "gemma": -0.07, "cohere": -0.07}


def detect(key: str) -> str | None:
    """Which free provider a pasted key belongs to (only providers whose keys have a prefix)."""
    for p in FREE_PROVIDERS.values():
        if p.key_pattern and re.match(p.key_pattern, key):
            return p.id
    return None


def pick_models(p: FreeProvider, ids: list[str]) -> list[tuple[str, ModelRule]]:
    """The provider's models worth using, best first, at most one per rule."""
    out: list[tuple[str, ModelRule]] = []
    for rule in p.rules:
        hits = sorted((i for i in ids if re.search(rule.pattern, i, re.I)), reverse=True)   # newest-looking first
        hits = [h for h in hits if not re.search(r"(guard|safety|embed|whisper|tts|orpheus|vision-only)", h, re.I)]
        hit = next((h for h in hits if h not in {m for m, _ in out}), None)
        if hit:
            out.append((hit, rule))
        if len(out) >= p.max_models:
            break
    return out


def limits_for(p: FreeProvider, rule: ModelRule | None) -> dict:
    d = dict(p.limits)
    if rule:
        d.update(rule.limits)
    return d

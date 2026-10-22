# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Free-tier image and video generation. Author: Akshay Dubey

Providers are tried best-first; each one needs only a free account's key
(Pollinations needs nothing at all). A provider that fails is skipped and the
reason is collected, so the user gets either a file or a clear explanation —
never a silent fall-through to a paid lane.
"""
from __future__ import annotations

import base64
import time
import urllib.parse

import httpx

from . import catalog, secrets

# (id, label, kinds, key env vars it needs — empty means no account required)
# "paid": the provider may bill once free quotas run out — used only after the
# user opts in (Studio > "allow paid image models"), matching the app's
# free-only-until-you-say-otherwise promise.
PROVIDERS = [
    {"id": "gemini", "label": "Google AI Studio", "kinds": {"image"}, "keys": ("GEMINI_API_KEY",),
     "paid": True},
    {"id": "huggingface", "label": "Hugging Face (free)", "kinds": {"image", "video"}, "keys": ("HF_TOKEN",)},
    {"id": "together", "label": "Together AI (free FLUX)", "kinds": {"image"}, "keys": ("TOGETHER_API_KEY",)},
    {"id": "cloudflare", "label": "Cloudflare Workers AI (free)",
     "kinds": {"image"}, "keys": ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")},
    {"id": "pollinations", "label": "Pollinations (free, public — avoid private prompts)",
     "kinds": {"image"}, "keys": ()},
]

# Newest first; a quota/error on one falls through to the next.
_GEMINI_IMAGE_MODELS = ("gemini-3.1-flash-image", "gemini-3-pro-image", "gemini-2.5-flash-image")
_HF_IMAGE_MODELS = ("black-forest-labs/FLUX.1-schnell", "black-forest-labs/FLUX.1-dev",
                    "stabilityai/stable-diffusion-xl-base-1.0")
_HF_VIDEO_MODELS = ("Lightricks/LTX-Video", "genmo/mochi-1-preview", "damo-vilab/text-to-video-ms-1.7b")


def status() -> list[dict]:
    """Per provider: what it can do and whether its keys are connected."""
    paid_ok = bool(catalog.get_settings().get("paid_media"))
    return [{"id": p["id"], "label": p["label"], "kinds": sorted(p["kinds"]),
             "connected": all(secrets.has(k) for k in p["keys"]), "free": not p.get("paid"),
             "enabled": paid_ok or not p.get("paid")}
            for p in PROVIDERS]


def _last_err(errs: list[str], kind: str) -> RuntimeError:
    missing = [p["label"] for p in PROVIDERS if kind in p["kinds"]
               and not all(secrets.has(k) for k in p["keys"])]
    hint = ""
    if missing and kind == "video":
        hint = (" Free video generation needs a Hugging Face token (free) — "
                "add it in Connections under Image & video keys.")
    joined = "; ".join(errs[-3:])
    return RuntimeError(f"No free {kind} provider could make this. {joined}{hint}".strip())


def generate(kind: str, prompt: str, width: int = 1024, height: int = 1024) -> tuple[bytes, str, str]:
    """Returns (data, mime_type, provider_id). Raises RuntimeError listing why each provider failed."""
    prompt = prompt.strip()
    if not prompt:
        raise RuntimeError("Describe what to generate first.")
    errs: list[str] = []
    paid_ok = bool(catalog.get_settings().get("paid_media"))
    for p in PROVIDERS:
        if kind not in p["kinds"] or not all(secrets.has(k) for k in p["keys"]):
            continue
        if p.get("paid") and not paid_ok:
            errs.append(f"{p['label']}: skipped — may bill your key once free quota runs out "
                        "(enable “Allow paid image models” in Studio to use it)")
            continue
        try:
            fn = _GENERATORS[(p["id"], kind)]
            data, mime = fn(prompt, width, height)
            if data and len(data) > 100:
                return data, mime, p["id"]
            errs.append(f"{p['label']}: empty reply")
        except Exception as e:
            errs.append(f"{p['label']}: {secrets.redact(str(e))[:140]}")
    raise _last_err(errs, kind)


# ---------------------------------------------------------------- providers
def _post(url: str, headers: dict, json: dict, timeout: float = 240) -> httpx.Response:
    r = httpx.post(url, headers=headers, json=json, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r


def _gemini_image(prompt: str, w: int, h: int) -> tuple[bytes, str]:
    key = secrets.get("GEMINI_API_KEY")
    last: Exception | None = None
    for model in _GEMINI_IMAGE_MODELS:
        try:
            r = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      {"x-goog-api-key": key},
                      {"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseModalities": ["IMAGE"]}}, timeout=180)
            for part in (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", []):
                blob = part.get("inlineData") or part.get("inline_data") or {}
                if blob.get("data"):
                    return base64.b64decode(blob["data"]), blob.get("mimeType", "image/png")
            last = RuntimeError(f"{model} returned no image")
        except Exception as e:
            last = e
    raise last or RuntimeError("no image model answered")


def _hf_call(model: str, prompt: str, w: int, h: int) -> httpx.Response:
    key = secrets.get("HF_TOKEN")
    url = f"https://router.huggingface.co/hf-inference/models/{model}"
    body = {"inputs": prompt, "parameters": {"width": w, "height": h}}
    r = httpx.post(url, headers={"authorization": f"Bearer {key}"}, json=body,
                   timeout=300, follow_redirects=True)
    if r.status_code == 503:                                   # cold model: wait out its estimate once
        est = 20
        try:
            est = min(25, float(r.json().get("estimated_time") or 20))
        except Exception:
            pass
        time.sleep(max(3, est))
        r = httpx.post(url, headers={"authorization": f"Bearer {key}"}, json=body,
                       timeout=300, follow_redirects=True)
    r.raise_for_status()
    return r


def _hf_image(prompt: str, w: int, h: int) -> tuple[bytes, str]:
    last: Exception | None = None
    for model in _HF_IMAGE_MODELS:
        try:
            r = _hf_call(model, prompt, w, h)
            if (r.headers.get("content-type") or "").startswith("image"):
                return r.content, r.headers["content-type"].split(";")[0]
            last = RuntimeError(f"{model} replied with JSON, not an image")
        except Exception as e:
            last = e
    raise last or RuntimeError("no image model answered")


def _hf_video(prompt: str, w: int, h: int) -> tuple[bytes, str]:
    last: Exception | None = None
    for model in _HF_VIDEO_MODELS:
        try:
            r = _hf_call(model, prompt, w, h)
            ct = (r.headers.get("content-type") or "").split(";")[0]
            if ct.startswith("video") or ct == "application/octet-stream":
                return r.content, "video/mp4"
            last = RuntimeError(f"{model} replied with JSON, not a video")
        except Exception as e:
            last = e
    raise last or RuntimeError("no video model answered")


def _together_image(prompt: str, w: int, h: int) -> tuple[bytes, str]:
    r = _post("https://api.together.xyz/v1/images/generations",
              {"authorization": f"Bearer {secrets.get('TOGETHER_API_KEY')}"},
              {"model": "black-forest-labs/FLUX.1-schnell-Free", "prompt": prompt,
               "width": w, "height": h, "steps": 4, "n": 1, "response_format": "b64_json"}, timeout=180)
    b64 = (r.json().get("data") or [{}])[0].get("b64_json", "")
    return base64.b64decode(b64), "image/png"


def _cloudflare_image(prompt: str, w: int, h: int) -> tuple[bytes, str]:
    acct = secrets.get("CLOUDFLARE_ACCOUNT_ID")
    r = _post(f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/black-forest-labs/flux-1-schnell",
              {"authorization": f"Bearer {secrets.get('CLOUDFLARE_API_TOKEN')}"},
              {"prompt": prompt, "width": w, "height": h}, timeout=180)
    ct = (r.headers.get("content-type") or "").split(";")[0]
    if ct.startswith("image"):
        return r.content, ct
    d = r.json()                                                   # some accounts return JSON-wrapped image
    img = (d.get("result") or {}).get("image")
    if img:
        return base64.b64decode(img), "image/png"
    raise RuntimeError("replied with no image")


def _pollinations_image(prompt: str, w: int, h: int) -> tuple[bytes, str]:
    q = urllib.parse.quote(prompt, safe="")
    url = f"https://image.pollinations.ai/prompt/{q}?width={w}&height={h}&model=flux&nologo=true"
    r = httpx.get(url, timeout=180, follow_redirects=True)
    r.raise_for_status()
    return r.content, (r.headers.get("content-type") or "image/jpeg").split(";")[0]


_GENERATORS = {
    ("gemini", "image"): _gemini_image,
    ("huggingface", "image"): _hf_image,
    ("huggingface", "video"): _hf_video,
    ("together", "image"): _together_image,
    ("cloudflare", "image"): _cloudflare_image,
    ("pollinations", "image"): _pollinations_image,
}

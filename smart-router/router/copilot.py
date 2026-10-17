# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""GitHub Copilot plan lane. Author: Akshay Dubey

If the person has a Copilot plan they sign in with GitHub's OAuth device flow
(the same flow the Copilot editor plugins use). Promptaro then exchanges the
GitHub token for short-lived Copilot service tokens and talks to the Copilot
chat endpoint. If nobody signs in, no Copilot models enter the catalog and
routing simply bypasses this lane.

Only the short-lived service token ever reaches api.githubcopilot.com; the
GitHub OAuth token stays in the OS keychain.
"""
from __future__ import annotations

import os
import time

import httpx

from . import secrets

# GitHub's public OAuth app id used by the Copilot editor plugins. Override for
# self-hosted/other OAuth apps; never a secret (it's embedded in public clients).
CLIENT_ID = os.environ.get("PROMPTARO_COPILOT_CLIENT_ID", "Iv1.b507a08c87ecfe98")
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
SERVICE_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
DEFAULT_API = "https://api.githubcopilot.com"
TOKEN_NAME = "GITHUB_COPILOT_TOKEN"

_device: dict = {}                    # pending device flow, one at a time
_svc: dict = {}                       # cached service token {"token", "expires_at", "api"}

UA = "Promptaro/1.0"
INTEGRATION_HEADERS = {
    "copilot-integration-id": "vscode-chat",
    "editor-version": "vscode/1.95.0",
    "editor-plugin-version": "copilot-chat/0.26.7",
    "openai-intent": "conversation-panel",
    "user-agent": UA,
}


def connected() -> bool:
    return secrets.has(TOKEN_NAME)


def disconnect() -> None:
    secrets.delete(TOKEN_NAME)
    _device.clear()
    _svc.clear()


def _api_base() -> str:
    from . import catalog                            # deferred: catalog imports this module
    return _svc.get("api") or catalog._read_cache("copilot_api.json", 10 ** 9) or DEFAULT_API


def start_device(client: httpx.Client | None = None) -> dict:
    """Ask GitHub for a device code the person enters in their browser."""
    r = (client or httpx.Client(timeout=15)).post(
        DEVICE_CODE_URL, data={"client_id": CLIENT_ID, "scope": "read:user"},
        headers={"accept": "application/json"})
    r.raise_for_status()
    d = r.json()
    _device.clear()
    _device.update({"device_code": d["device_code"], "expires": time.time() + d.get("expires_in", 900),
                    "interval": d.get("interval", 5)})
    return {"user_code": d["user_code"], "verification_uri": d.get("verification_uri",
            "https://github.com/login/device"), "interval": _device["interval"],
            "expires_in": int(_device["expires"] - time.time())}


def poll_device(client: httpx.Client | None = None) -> dict:
    """One poll of the device grant. Returns {"status": pending|connected|error}."""
    if not _device or _device["expires"] < time.time():
        _device.clear()
        return {"status": "error", "detail": "Sign-in expired. Start again."}
    r = (client or httpx.Client(timeout=15)).post(
        ACCESS_TOKEN_URL, headers={"accept": "application/json"},
        data={"client_id": CLIENT_ID, "device_code": _device["device_code"],
              "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    r.raise_for_status()
    d = r.json()
    err = d.get("error")
    if err in ("authorization_pending", "slow_down"):
        if err == "slow_down":
            _device["interval"] += 5
        return {"status": "pending", "interval": _device["interval"]}
    if err:
        _device.clear()
        return {"status": "error", "detail": d.get("error_description") or err}
    secrets.set(TOKEN_NAME, d["access_token"])
    _device.clear()
    _svc.clear()
    return {"status": "connected"}


def _exchange(resp: httpx.Response) -> tuple[str, str]:
    resp.raise_for_status()
    d = resp.json()
    token, api = d.get("token", ""), (d.get("endpoints") or {}).get("api") or DEFAULT_API
    if not token:
        raise RuntimeError("GitHub did not return a Copilot token (is Copilot enabled on this account?)")
    _svc.update({"token": token, "expires_at": d.get("expires_at", time.time() + 1500), "api": api})
    from . import catalog                            # deferred: catalog imports this module
    catalog._write_cache("copilot_api.json", api)
    return token, api


def service_token(client: httpx.Client) -> tuple[str, str]:
    """Sync variant for the catalog/discovery thread."""
    if _svc.get("token") and _svc["expires_at"] - 60 > time.time():
        return _svc["token"], _svc["api"]
    gh = secrets.get(TOKEN_NAME)
    if not gh:
        raise RuntimeError("GitHub Copilot is not signed in")
    r = client.get(SERVICE_TOKEN_URL, headers={"authorization": f"token {gh}", "accept": "application/json",
                                               "user-agent": UA})
    return _exchange(r)


async def aservice_token(client: httpx.AsyncClient) -> tuple[str, str]:
    """Short-lived Copilot service token + API base, cached until ~60s before expiry."""
    if _svc.get("token") and _svc["expires_at"] - 60 > time.time():
        return _svc["token"], _svc["api"]
    gh = secrets.get(TOKEN_NAME)
    if not gh:
        raise RuntimeError("GitHub Copilot is not signed in")
    r = await client.get(SERVICE_TOKEN_URL,
                         headers={"authorization": f"token {gh}", "accept": "application/json",
                                  "user-agent": UA})
    return _exchange(r)


def chat_headers(token: str) -> dict:
    return {"authorization": f"Bearer {token}", "content-type": "application/json",
            **INTEGRATION_HEADERS}


def list_models(client: httpx.Client | None = None) -> list[str]:
    """Chat-capable model ids visible to this Copilot plan."""
    token, api = service_token(client or httpx.Client(timeout=15))
    r = (client or httpx.Client(timeout=15)).get(f"{api}/models", headers=chat_headers(token))
    r.raise_for_status()
    out = []
    for m in r.json().get("data", []):
        mid, caps = m.get("id", ""), m.get("capabilities") or {}
        typ = caps.get("type") if isinstance(caps, dict) else None
        if mid and (not typ or typ == "chat"):
            out.append(mid)
    return out


def _reset() -> None:
    """Test hook: drop in-memory caches (keeps the stored GitHub token)."""
    _device.clear()
    _svc.clear()

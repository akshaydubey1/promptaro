# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Credential storage. Author: Akshay Dubey

Keys are kept in the operating system's credential store (macOS Keychain,
Windows Credential Manager, Linux Secret Service) via `keyring`. Nothing is
written to the project folder. Order of lookup:

  1. environment variable (lets Docker/CI keep working)
  2. OS keychain (service "promptaro"; Tillwright-era "akshay-smart-router" is
     still read so existing installs keep working)
  3. ~/.promptaro/credentials.json (0600) — only if no keychain exists

Values are never logged or returned by the API; only "connected: yes/no".
"""
from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

SERVICE = "promptaro"
_LEGACY_SERVICE = "akshay-smart-router"
_FALLBACK = Path(os.environ.get("PROMPTARO_HOME") or os.environ.get("ROUTER_HOME")
                 or Path.home() / ".promptaro") / "credentials.json"
_LEGACY_FALLBACK = Path(os.environ.get("ROUTER_HOME") or Path.home() / ".akshay-router") / "credentials.json"
_lock = threading.Lock()

try:
    import keyring
    from keyring.errors import KeyringError

    _kr = keyring.get_keyring()
    # The "fail"/"null" backends report priority 0; a real keychain is > 0.
    _kr_ok = float(getattr(_kr, "priority", 0) or 0) > 0 and "fail" not in type(_kr).__module__
except Exception:  # keyring missing or broken
    keyring, KeyringError, _kr_ok = None, Exception, False

if os.environ.get("ROUTER_SECRETS_BACKEND") == "file":
    _kr_ok = False


def backend() -> str:
    return "os-keychain" if _kr_ok else "file"


def _file_read() -> dict:
    try:
        return json.loads(_FALLBACK.read_text())
    except Exception:
        return {}


def _file_write(d: dict) -> None:
    _FALLBACK.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(_FALLBACK.parent, stat.S_IRWXU)
    tmp = _FALLBACK.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(_FALLBACK)


def get(name: str) -> str:
    if not name:
        return ""
    v = os.environ.get(name)
    if v:
        return v
    if _kr_ok:
        for service in (SERVICE, _LEGACY_SERVICE):
            try:
                v = keyring.get_password(service, name)
            except KeyringError:
                v = None
            if v:
                return v
        return ""
    if v := _file_read().get(name):                            # noqa: E501 — read fallback file first
        return v
    try:
        return json.loads(_LEGACY_FALLBACK.read_text()).get(name, "")
    except Exception:
        return ""


def set(name: str, value: str) -> None:  # noqa: A001
    with _lock:
        if _kr_ok:
            try:
                keyring.set_password(SERVICE, name, value)
                try:
                    keyring.delete_password(_LEGACY_SERVICE, name)   # shadow the old entry next time
                except Exception:
                    pass
                return
            except KeyringError:
                pass
        d = _file_read()
        d[name] = value
        _file_write(d)


def delete(name: str) -> None:
    with _lock:
        if _kr_ok:
            for service in (SERVICE, _LEGACY_SERVICE):
                try:
                    keyring.delete_password(service, name)
                except Exception:
                    pass
        d = _file_read()
        if d.pop(name, None) is not None:
            _file_write(d)


def has(name: str) -> bool:
    return bool(get(name))


def anthropic_headers(key: str | None = None) -> dict:
    """Headers for Anthropic's API. Organization-scoped keys must name a workspace on every request."""
    h = {"x-api-key": key if key is not None else get("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"}
    ws = get("ANTHROPIC_WORKSPACE_ID")
    if ws:
        h["anthropic-workspace-id"] = ws
    return h

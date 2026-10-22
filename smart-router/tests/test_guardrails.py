# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Guardrails — properties that must never regress.

These tests exist so a refactor or a new provider can't quietly weaken the
things the rest of the suite relies on: no credential-shaped strings committed
to the repo, redaction covering every key shape, removed lanes staying gone,
and defaults that fail closed (free-only, chat-scoped free models).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from router import catalog, free_providers, secrets

REPO = Path(__file__).resolve().parents[2]      # repository root (smart-router's parent)


def _tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / p for p in out.stdout.splitlines() if p.strip()]


def _read(f: Path) -> str | None:
    try:
        return f.read_text()
    except (UnicodeDecodeError, OSError):
        return None


# ------------------------------------------------------------------ leaks
def test_no_secret_shaped_strings_committed():
    """A pasted key must never make it into a tracked file.

    tests/ is exempt: detection suites deliberately carry key-shaped fakes."""
    hits = []
    for f in _tracked_files():
        if "tests" in f.relative_to(REPO).parts:
            continue
        text = _read(f)
        if text is None:
            continue
        for m in secrets._SECRET_TEXT.finditer(text):
            hits.append(f"{f.relative_to(REPO)}: {m.group(0)[:7]}…")
    assert hits == []


def test_redact_truncates_every_known_key_shape():
    samples = [
        "sk-or-v1-" + "a" * 40, "sk-ant-" + "b" * 40, "sk-proj-" + "c" * 40,
        "sk-" + "d" * 40, "gsk_" + "e" * 40, "csk-" + "f" * 40,
        "nvapi-" + "g" * 40, "AIza" + "h" * 40, "xoxb-" + "i" * 40,
        "AKIA" + "J" * 16, "ghp_" + "k" * 40, "github_pat_" + "l" * 40,
    ]
    for key in samples:
        red = secrets.redact(f"failed for {key} after 3 tries")
        assert key not in red, f"{key[:10]}… survived redaction"
    assert secrets.redact("plain error, nothing sensitive") == "plain error, nothing sensitive"


def test_removed_lanes_and_attribution_stay_gone():
    """Provider lanes removed for policy/safety reasons must not creep back in,
    and commit-generated attribution strings stay out of tracked files."""
    banned = ["github_" + "copilot", "api.copilot.github" + ".com", "github_models",
              "Generated " + "with ", "Co-Authored" + "-By:"]
    hits = []
    for f in _tracked_files():
        if f.name == "test_guardrails.py":
            continue                                   # this file carries the strings it hunts
        text = _read(f)
        if text is None:
            continue
        for b in banned:
            if b in text:
                hits.append(f"{f.relative_to(REPO)}: {b}")
    assert hits == []


# ------------------------------------------------------------------ defaults
def test_defaults_fail_closed(isolated_secrets, monkeypatch):
    """With a fresh settings file, money must stay off and free code stays private."""
    monkeypatch.delenv("PROMPTARO_ALLOW_PAID", raising=False)   # tests opt in; real installs don't
    s = catalog.get_settings()
    assert s["allow_paid"] is False                     # paid per-use models off: $0 by default
    assert s.get("gateway_spend", "free") == "free"    # paid models refuse until explicitly enabled
    assert s.get("free_models", "chat") == "chat"      # code never routes to trains-flagged free tiers
    assert float(s.get("daily_budget_usd") or 0) == 0.0


def test_free_providers_all_carry_privacy_and_limit_metadata():
    """Every free provider must declare whether it may train on prompts and its
    real limits — a provider missing either is a silent privacy/quota bug."""
    for p in free_providers.FREE_PROVIDERS.values():
        assert isinstance(p.trains, bool), f"{p.id} missing a trains flag"
        assert p.limits, f"{p.id} declares no rate limits — quota tracking would lie"
        assert p.label and p.get_key_url, f"{p.id} missing connection metadata"


def test_free_model_specs_carry_the_providers_privacy_flag_and_quota_lane(isolated_secrets):
    """Every $0 spec must be marked free, faithfully carry its provider's trains
    flag, and sit in a quota lane — otherwise privacy rules and the ledger are
    blind to it."""
    secrets.set("GROQ_API_KEY", "gsk_" + "x" * 20)
    specs = catalog.free_provider_specs()
    assert specs, "a connected free provider produced no specs"
    flag = free_providers.FREE_PROVIDERS["groq"].trains
    for s in specs:
        assert s["price"]["input"] == 0.0 and s["price"]["output"] == 0.0
        assert s["free"] is True
        assert s["trains"] is flag, f"{s['id']} mislabels its provider's trains flag"
        assert s.get("quota"), f"{s['id']} has no quota lane — it would bypass the free ledger"


# ------------------------------------------------------------------ privacy and ownership
_PERSONAL = re.compile(r"[A-Za-z0-9._%+-]+@(gmail|googlemail|yahoo|outlook|hotmail|icloud|proton|protonmail)\.com"
                       r"|/Users/(?!you/|me/|name/|username/|example/)[a-z][a-z0-9_-]+/|C:\\\\Users\\\\[a-z]|/home/(?!u/|user/|runner/)[a-z][a-z0-9_-]+/", re.I)


def test_no_personal_emails_or_home_paths_in_tracked_files():
    """Personal email addresses and real home-folder paths never ship in the repo."""
    hits = []
    for f in _tracked_files():
        if f.suffix in (".png", ".jpg", ".ico", ".icns") or f.name == "test_guardrails.py":
            continue
        text = _read(f)
        if text is None:
            continue
        for m in _PERSONAL.finditer(text):
            hits.append(f"{f.relative_to(REPO)}: {m.group(0)}")
    assert hits == []


_CODE = (".py", ".sh", ".ps1", ".bat", ".command", ".js", ".css", ".html", ".yml", ".yaml")


def test_every_source_file_carries_the_license_header():
    """Ownership and licence are stated in every source file, and say the same thing."""
    missing, stale = [], []
    for f in _tracked_files():
        if f.suffix not in _CODE or "vendor" in f.parts or ".github" in f.parts:
            continue
        head = (_read(f) or "")[:600]
        if "Akshay Dubey" not in head:
            missing.append(str(f.relative_to(REPO)))
        if "Proprietary and confidential" in head or "All rights reserved" in head:
            stale.append(str(f.relative_to(REPO)))
    assert missing == [] and stale == [], f"missing: {missing[:5]} stale: {stale[:5]}"


def test_license_is_polyform_small_business_everywhere():
    for lic in ("LICENSE", "promptaro/LICENSE", "smart-router/LICENSE"):
        text = (REPO / lic).read_text()
        assert text.startswith("Required Notice: Copyright (c) 2026 Akshay Dubey"), lic
        assert "# PolyForm Small Business License 1.0.0" in text, lic


def _history() -> list[tuple[str, str, str]] | None:
    """(sha, author email, message) for every non-merge commit, or None without full history."""
    try:
        shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=REPO,
                                 capture_output=True, text=True, check=True).stdout.strip()
        if shallow == "true":
            return None
        out = subprocess.run(["git", "log", "--no-merges", "--format=%H%x1f%ae%x1f%B%x1e"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    rows = []
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) == 3:
            rows.append((parts[0][:8], parts[1], parts[2]))
    return rows


def test_commit_history_credits_only_the_author():
    """Every commit is authored by the owner's private GitHub address, with no AI co-author
    lines or tool footers. Fails loudly if a merge or a tool brings them back."""
    rows = _history()
    if rows is None:
        pytest.skip("shallow clone: full history not available")
    bad = []
    for sha, email, msg in rows:
        if not (email.endswith("+akshaydubey1@users.noreply.github.com") or email == "noreply@github.com"):
            bad.append(f"{sha}: author {email}")
        for line in msg.splitlines():
            if re.match(r"\s*(co-authored-by|claude-session):", line, re.I) or \
                    re.search(r"generated (with|by) \[?(claude|devin)", line, re.I):
                bad.append(f"{sha}: {line.strip()[:60]}")
    assert bad == [], bad[:10]


# ------------------------------------------------------------------ network exposure
def test_servers_only_listen_on_this_computer():
    """Launchers bind 127.0.0.1, and Docker publishes on loopback only."""
    for f in ("promptaro/launch.sh", "promptaro/packaging/start-promptaro.ps1", "smart-router/Start Smart Router.command"):
        text = (REPO / f).read_text()
        assert "127.0.0.1" in text and "0.0.0.0" not in text, f
    compose = (REPO / "smart-router/docker-compose.yml").read_text()
    assert '"127.0.0.1:8080:8080"' in compose

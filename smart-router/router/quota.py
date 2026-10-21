# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Free-tier ledger: know what's left before asking, never wait on a refusal. Author: Akshay Dubey

For every free provider (and model, where limits are per model) it keeps
requests and tokens used this minute, today (UTC) and this month, checks a
request against the provider's limits before it's sent, and learns from:

  * rate-limit headers, when the provider sends them (they beat our numbers);
  * a 429 refusal: that provider/model is paused for its Retry-After (or a
    minute, or until midnight UTC for daily limits) and routing moves on at
    once to the next model instead of retrying with back-off.

It also refuses requests that can't fit: a 25k-token coding-agent request is
never sent to a model whose free tier allows 8k tokens a minute.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

from .free_providers import FREE_PROVIDERS


def _day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _month() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def _to_midnight() -> float:
    t = time.gmtime()
    return 86400 - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)


def _to_month_end() -> float:
    """Seconds until the first day of next month, UTC."""
    t = time.gmtime()
    y, m = (t.tm_year, t.tm_mon + 1) if t.tm_mon < 12 else (t.tm_year + 1, 1)
    return calendar.timegm((y, m, 1, 0, 0, 0, 0, 0, 0)) - time.time()


_DUR = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")


def parse_reset(v: str | None) -> float | None:
    """'2m59.56s', '7.66s', '1h2m', '450ms', '12' (seconds) -> seconds."""
    if not v:
        return None
    v = v.strip()
    try:
        return float(v)
    except ValueError:
        pass
    m = _DUR.match(v)
    if not m or not any(m.groups()):
        return None
    h, mi, s, ms = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.lock = threading.Lock()
        self.minute: dict[str, deque] = {}            # scope -> deque[(ts, tokens)]
        self.paused: dict[str, tuple[float, str]] = {}   # scope -> (until, reason)
        self.live: dict[str, dict] = {}               # scope -> what the last headers said
        self.day, self.month = _day(), _month()
        self.daily: dict[str, list] = {}              # scope -> [requests, tokens]
        self.monthly: dict[str, list] = {}
        self._load()

    # ------------------------------------------------------------- storage
    def _load(self) -> None:
        if not self.path:
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if d.get("day") == self.day:
            self.daily = {k: list(v) for k, v in d.get("daily", {}).items()}
            self.paused = {k: (u, r) for k, (u, r) in d.get("paused", {}).items() if u > time.time()}
        if d.get("month") == self.month:
            self.monthly = {k: list(v) for k, v in d.get("monthly", {}).items()}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({"day": self.day, "month": self.month, "daily": self.daily,
                                       "monthly": self.monthly, "paused": self.paused}), encoding="utf-8")
            os.replace(tmp, self.path)                      # atomic: a crash never leaves a half-written ledger
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError:
            pass

    def _roll(self) -> None:
        if self.day != _day():
            self.day, self.daily = _day(), {}
            self.paused = {k: v for k, v in self.paused.items() if v[1] != "daily"}
        if self.month != _month():
            self.month, self.monthly = _month(), {}

    # ------------------------------------------------------------- scopes
    @staticmethod
    def scope(spec) -> str | None:
        """Ledger key for a model: 'groq:openai/gpt-oss-120b' or 'nvidia' (account-wide)."""
        pid = spec.extra.get("quota")
        p = FREE_PROVIDERS.get(pid or "")
        if not p:
            return None
        return pid if p.scope == "account" else f"{pid}:{spec.model}"

    @staticmethod
    def limits(spec) -> dict:
        return dict(spec.extra.get("limits") or FREE_PROVIDERS[spec.extra["quota"]].limits)

    def _minute(self, sc: str) -> tuple[int, int]:
        q = self.minute.setdefault(sc, deque())
        cut = time.time() - 60
        while q and q[0][0] < cut:
            q.popleft()
        return len(q), sum(t for _, t in q)

    # -------------------------------------------------------------- checks
    def _check(self, spec, est_tokens: int) -> tuple[bool, str]:
        """Read-only limit check. Caller holds self.lock."""
        sc = self.scope(spec)
        lim = self.limits(spec)
        label = FREE_PROVIDERS[spec.extra["quota"]].label
        until, why = self.paused.get(sc, (0, ""))
        if until > time.time():
            reason = {"daily": "daily limit reached", "monthly": "monthly limit reached"}.get(why,
                                                                                              "is rate-limiting")
            return False, f"{label} {reason}"
        req_m, tok_m = self._minute(sc)
        day = self.daily.get(sc, [0, 0])
        mon = self.monthly.get(sc, [0, 0])
        if lim.get("tpm") and est_tokens > lim["tpm"]:
            return False, f"request too large for {label}'s free tier ({lim['tpm']:,} tokens a minute)"
        if lim.get("rpm") and req_m >= lim["rpm"]:
            return False, f"{label} per-minute limit reached"
        if lim.get("tpm") and tok_m + est_tokens > lim["tpm"]:
            return False, f"{label} per-minute token limit reached"
        if lim.get("rpd") and day[0] >= lim["rpd"]:
            return False, f"{label} daily limit reached"
        if lim.get("tpd") and day[1] + est_tokens > lim["tpd"]:
            return False, f"{label} daily token limit reached"
        if lim.get("rpmonth") and mon[0] >= lim["rpmonth"]:
            return False, f"{label} monthly limit reached"
        live = self.live.get(sc, {})
        if live.get("requests_left") == 0 and live.get("requests_reset_at", 0) > time.time():
            return False, f"{label} limit reached (provider says)"
        if live.get("tokens_left") is not None and live.get("tokens_reset_at", 0) > time.time() \
                and est_tokens > live["tokens_left"]:
            return False, f"{label} token limit reached (provider says)"
        return True, ""

    def allow(self, spec, est_tokens: int = 0) -> tuple[bool, str]:
        sc = self.scope(spec)
        if not sc:
            return True, ""
        with self.lock:
            self._roll()
            return self._check(spec, est_tokens)

    # ----------------------------------------------------- reservations
    # A request counts against the free tier while it is in flight, not just
    # after it lands: concurrent requests see each other's usage. begin()
    # reserves (request + estimated tokens); settle() swaps the estimate for
    # the real usage; abandon() refunds a request that never reached the model.
    def begin(self, spec, est_tokens: int = 0) -> tuple[bool, str, dict | None]:
        sc = self.scope(spec)
        if not sc:
            return True, "", None
        with self.lock:
            self._roll()
            ok, why = self._check(spec, est_tokens)
            if not ok:
                return False, why, None
            self.minute.setdefault(sc, deque()).append((time.time(), est_tokens))
            d = self.daily.setdefault(sc, [0, 0])
            d[0], d[1] = d[0] + 1, d[1] + est_tokens
            mo = self.monthly.setdefault(sc, [0, 0])
            mo[0], mo[1] = mo[0] + 1, mo[1] + est_tokens
            self._save()
            return True, "", {"scope": sc, "est": est_tokens}

    def settle(self, res: dict | None, spec, tokens: int, headers: dict | None = None) -> None:
        """Reservation finished: correct the estimate with real usage + fresh rate-limit headers."""
        if not res:
            return
        sc = res["scope"]
        with self.lock:
            self._roll()
            delta = tokens - res["est"]
            if sc in self.daily:
                self.daily[sc][1] += delta
            if sc in self.monthly:
                self.monthly[sc][1] += delta
            if headers:
                self._note_headers(sc, spec, headers)
            self._save()

    def abandon(self, res: dict | None) -> None:
        """The call never completed (error/cancel): refund the request and token counts.
        The per-minute entry stays — a refused request still consumed provider-side slots."""
        if not res:
            return
        sc = res["scope"]
        with self.lock:
            self._roll()
            for table in (self.daily, self.monthly):
                if sc in table:
                    table[sc][0] = max(0, table[sc][0] - 1)
                    table[sc][1] = max(0, table[sc][1] - res["est"])
            self._save()

    def wait_for(self, spec, est_tokens: int = 0) -> float | None:
        """Seconds until this model can take the request, if only a per-minute window is in the way.
        None when it can't (too large, daily/monthly limit, paused for long)."""
        sc = self.scope(spec)
        if not sc:
            return 0.0
        with self.lock:
            self._roll()
            lim = self.limits(spec)
            if lim.get("tpm") and est_tokens > lim["tpm"]:
                return None
            day = self.daily.get(sc, [0, 0])
            if (lim.get("rpd") and day[0] >= lim["rpd"]) or (lim.get("tpd") and day[1] + est_tokens > lim["tpd"]):
                return None
            now = time.time()
            until, why = self.paused.get(sc, (0, ""))
            wait = max(0.0, until - now) if why == "minute" else (None if until > now else 0.0)
            if wait is None:
                return None
            q = list(self.minute.get(sc, ()))
            # drop the oldest entries until both minute limits are satisfied
            req_lim, tok_lim = lim.get("rpm"), lim.get("tpm")
            while q and ((req_lim and len(q) >= req_lim) or (tok_lim and sum(t for _, t in q) + est_tokens > tok_lim)):
                ts, _ = q.pop(0)
                wait = max(wait, ts + 60 - now)
            return wait

    # ------------------------------------------------------------- updates
    def _note_headers(self, sc: str, spec, headers: dict) -> None:
        """Trust the provider's own rate-limit headers over our counters. Caller holds self.lock."""
        p = FREE_PROVIDERS[spec.extra["quota"]]
        h = {k.lower(): v for k, v in headers.items()}
        live = self.live.setdefault(sc, {})
        now = time.time()
        for kind, unit in (("requests", p.header_requests), ("tokens", p.header_tokens)):
            left = h.get(f"x-ratelimit-remaining-{kind}")
            if left is None:
                continue
            try:
                live[f"{kind}_left"] = int(float(left))
            except ValueError:
                continue
            reset = parse_reset(h.get(f"x-ratelimit-reset-{kind}"))
            live[f"{kind}_reset_at"] = now + (reset if reset is not None else (60 if unit == "minute" else _to_midnight()))
            live[f"{kind}_unit"] = unit
            lim = h.get(f"x-ratelimit-limit-{kind}")
            if lim:
                try:
                    live[f"{kind}_limit"] = int(float(lim))
                except ValueError:
                    pass

    def record(self, spec, tokens: int, headers: dict | None = None) -> None:
        """Count a completed call that wasn't reserved first (kept for one-shot callers)."""
        sc = self.scope(spec)
        if not sc:
            return
        with self.lock:
            self._roll()
            self.minute.setdefault(sc, deque()).append((time.time(), tokens))
            d = self.daily.setdefault(sc, [0, 0])
            d[0], d[1] = d[0] + 1, d[1] + tokens
            mo = self.monthly.setdefault(sc, [0, 0])
            mo[0], mo[1] = mo[0] + 1, mo[1] + tokens
            if headers:
                self._note_headers(sc, spec, headers)
            self._save()

    def refused(self, spec, retry_after: float | None = None, message: str = "") -> None:
        """A 429 (or 413 'too large'). Pause just this provider/model and move on."""
        sc = self.scope(spec)
        if not sc:
            return
        msg = message.lower()
        with self.lock:
            daily = any(w in msg for w in ("per day", "per-day", "daily", "(rpd)", "(tpd)")) or \
                ("quota" in msg and "month" not in msg and "minute" not in msg)
            if daily:
                self.paused[sc] = (time.time() + _to_midnight(), "daily")
            elif "month" in msg:
                self.paused[sc] = (time.time() + min(_to_month_end(), 45 * 86400), "monthly")
            else:
                wait = retry_after if retry_after and retry_after > 0 else 60
                self.paused[sc] = (time.time() + min(wait, 3600), "minute")
            self._save()

    # ------------------------------------------------------------ dashboard
    def snapshot(self, specs: list) -> list[dict]:
        """Per connected free provider: used today, limits, what's left, paused or not."""
        out: dict[str, dict] = {}
        with self.lock:
            self._roll()
            for s in specs:
                sc = self.scope(s)
                if not sc:
                    continue
                pid = s.extra["quota"]
                p = FREE_PROVIDERS[pid]
                lim = self.limits(s)
                row = out.setdefault(pid, {"id": pid, "label": p.label, "trains": p.trains, "models": [],
                                           "requests_today": 0, "tokens_today": 0, "tokens_left_today": 0,
                                           "requests_left_today": 0, "paused": "", "note": p.note,
                                           "checked": p.checked, "source": p.source})
                day = self.daily.get(sc, [0, 0])
                until, why = self.paused.get(sc, (0, ""))
                model_row = {"model": s.extra.get("name", s.model), "requests_today": day[0], "tokens_today": day[1],
                             "limits": lim, "paused_for_s": max(0, int(until - time.time())) if until else 0,
                             "pause_reason": why if until > time.time() else ""}
                live = self.live.get(sc, {})
                if live.get("requests_unit") == "day" and live.get("requests_left") is not None:
                    model_row["requests_left_today"] = live["requests_left"]
                row["models"].append(model_row)
                if p.scope == "account":                  # shared counters: count once
                    row["requests_today"], row["tokens_today"] = day
                else:
                    row["requests_today"] += day[0]
                    row["tokens_today"] += day[1]
                if lim.get("tpd"):
                    row["tokens_left_today"] += max(0, lim["tpd"] - day[1]) if p.scope == "model" or not row["models"][:-1] else 0
                if lim.get("rpd"):
                    left = model_row.get("requests_left_today", max(0, lim["rpd"] - day[0]))
                    row["requests_left_today"] += left if p.scope == "model" or len(row["models"]) == 1 else 0
                if until > time.time() and not row["paused"]:
                    row["paused"] = why
        return list(out.values())


LEDGER = Ledger()


def after_call(spec, comp) -> None:
    """Count a successful call against the free tier."""
    if spec.extra.get("quota"):
        LEDGER.record(spec, int(comp.tokens_in) + int(comp.tokens_out), getattr(comp, "headers", None))


def after_error(spec, err: Exception) -> bool:
    """True if the error was a free-tier refusal (pause and move on; not the model's fault)."""
    if not spec.extra.get("quota"):
        return False
    status = getattr(err, "status", None)
    text = str(err)
    if status in (429, 413):
        LEDGER.refused(spec, getattr(err, "retry_after", None), text)
        return True
    # Some providers wrap limit errors in a 400: only trust limit-shaped text when
    # the status is unknown or plausibly a refusal — never on a plain 5xx.
    if status in (None, 400, 402, 403) and re.search(
            r"\b429\b|rate.?limit|resource.?exhausted|quota|too (many|large)", text.lower()):
        LEDGER.refused(spec, getattr(err, "retry_after", None), text)
        return True
    return False


def use(path: Path) -> Ledger:
    """Point the shared ledger at a file (done once at startup)."""
    global LEDGER
    LEDGER = Ledger(path)
    return LEDGER

# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Send only the relevant part of large context. Author: Akshay Dubey

Splits context into chunks (paragraphs / fenced code blocks), scores each by
term overlap with the instruction, and keeps the best chunks in their original
order until the token budget is reached. Short context passes through intact.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .classifier import estimate_tokens

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOP = set("the and for with that this from are was were have has not but you your our can will "
            "into about there their them then than what when which who how all any its".split())


def _terms(text: str) -> Counter:
    return Counter(w.lower() for w in _WORD.findall(text) if w.lower() not in _STOP)


def _chunks(text: str) -> list[str]:
    parts, buf, in_code = [], [], False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        buf.append(line)
        if not in_code and line.strip() == "":
            parts.append("".join(buf))
            buf = []
    if buf:
        parts.append("".join(buf))
    # Merge tiny fragments so we don't lose surrounding meaning.
    merged: list[str] = []
    for p in parts:
        if merged and estimate_tokens(merged[-1]) < 60:
            merged[-1] += p
        else:
            merged.append(p)
    return [p for p in merged if p.strip()]


def fit_context(instruction: str, context: str, budget_tokens: int) -> tuple[str, dict]:
    total = estimate_tokens(context)
    if total <= budget_tokens:
        return context, {"context_tokens_in": total, "context_tokens_sent": total, "trimmed": False}
    q = _terms(instruction)
    chunks = _chunks(context)
    df = Counter()
    chunk_terms = [_terms(c) for c in chunks]
    for ct in chunk_terms:
        df.update(set(ct))
    n = len(chunks)
    scored = []
    for i, (c, ct) in enumerate(zip(chunks, chunk_terms)):
        s = sum(min(ct[t], 3) * math.log(1 + n / (1 + df[t])) for t in q)
        s /= math.sqrt(1 + estimate_tokens(c) / 200)     # don't reward sheer length
        if i == 0:
            s += 0.5                                     # headers/intro are usually useful
        scored.append((s, i))
    keep, used = set(), 0
    for s, i in sorted(scored, reverse=True):
        t = estimate_tokens(chunks[i])
        if used + t > budget_tokens:
            continue
        keep.add(i)
        used += t
    out, last = [], -1
    for i in sorted(keep):
        if last != -1 and i != last + 1:
            out.append("\n[... omitted as less relevant ...]\n\n")
        out.append(chunks[i])
        last = i
    trimmed = "".join(out)
    return trimmed, {"context_tokens_in": total, "context_tokens_sent": estimate_tokens(trimmed), "trimmed": True}

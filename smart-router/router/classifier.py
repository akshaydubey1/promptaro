# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Fast, local task classification. No model call, no network.

Author: Akshay Dubey

Turns one plain-language instruction into a TaskProfile: what kind of work it
is, how hard it looks, which capabilities it needs, and how confident the
classifier is. Precompiled regexes keep this well under a millisecond.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TASK_TYPES = ("coding", "research", "writing", "document", "data", "chat")

_SIGNALS: dict[str, list[tuple[str, float]]] = {
    "coding": [
        (r"\b(code|function|class\s+\w+|method|bug|debug|refactor|unit ?tests?|compile|stack ?trace|exception)\b", 2.0),
        (r"\b(python|javascript|typescript|golang|rust|java|c\+\+|bash|sql|terraform|helm|kubernetes|k8s|dockerfile|yaml|regex|api endpoint)\b", 1.5),
        (r"```|def |=>|\bimport \w+|\bgoroutine|go\.mod|\bgo (test|build|run|install)\b", 2.5),
        (r"\b(implement|write a script|fix the|pull request|pr review|lint)\b", 1.5),
        (r"\b(architect(ure)?|system design|pipeline|microservices?|infrastructure|deploy(ment)?|cluster|database schema)\b", 1.5),
    ],
    "research": [
        (r"\b(research|investigate|compare|comparison|sources?|citations?|literature|latest|current|news|market|benchmark(s)?|trends?)\b", 2.0),
        (r"\b(what is the state of|pros and cons|evidence|studies|according to)\b", 1.5),
    ],
    "writing": [
        (r"\b(write|draft|rewrite|polish|edit|email|blog|post|essay|cover letter|linkedin|tone|story|announcement)\b", 1.5),
        (r"\b(summari[sz]e for|make it sound|proofread)\b", 1.5),
    ],
    "document": [
        (r"\b(pdf|docx|word document|spreadsheet|xlsx|slides?|deck|pptx|report|contract|resume|invoice)\b", 2.0),
        (r"\b(extract|fill (in|out)|convert .* to|attached|this file)\b", 1.5),
    ],
    "data": [
        (r"\b(csv|dataset|dataframe|pandas|chart|plot|graph|analy[sz]e (the )?data|statistics|regression|average|median)\b", 2.0),
        (r"\b(json schema|parse|aggregate|pivot)\b", 1.0),
    ],
}

_COMPLEXITY_UP = re.compile(
    r"\b(architect|design (a|the) system|end[- ]to[- ]end|production|distributed|multi[- ]step|"
    r"thorough|comprehensive|in[- ]depth|security|migrat(e|ion)|optimi[sz]e|prove|edge cases|"
    r"concurren|scal(e|able|ing)|trade[- ]?offs?)\b",
    re.I,
)
_COMPLEXITY_DOWN = re.compile(r"\b(quick|simple|short|one[- ]line|brief|tl;?dr|just|typo|rename)\b", re.I)
_NEEDS_JSON = re.compile(r"\b(json|structured output|(?:output|response|json) schema)\b", re.I)
_NEEDS_WEB = re.compile(r"\b(latest|today|current(ly)?|news|this week|202[5-9]|price of|who is the)\b", re.I)
_NEEDS_TESTS = re.compile(r"\b(tests?|pytest|unit test|make sure it (works|runs)|verify)\b", re.I)
_SQL = re.compile(r"\bSELECT\b[\s\S]{0,200}?\bFROM\b")   # case-sensitive: prose "select ... from" doesn't count

_COMPILED = {t: [(re.compile(p, re.I), w) for p, w in sigs] for t, sigs in _SIGNALS.items()}


@dataclass
class TaskProfile:
    task_type: str
    complexity: float            # 0.0 (trivial) .. 1.0 (very hard)
    confidence: float            # 0.0 .. 1.0 how sure the classifier is
    needs: set[str] = field(default_factory=set)   # capabilities: json, web, tools, long_context, vision
    scores: dict[str, float] = field(default_factory=dict)
    est_input_tokens: int = 0

    def as_dict(self) -> dict:
        return {
            "task_type": self.task_type,
            "complexity": round(self.complexity, 3),
            "confidence": round(self.confidence, 3),
            "needs": sorted(self.needs),
            "scores": {k: round(v, 2) for k, v in self.scores.items()},
            "est_input_tokens": self.est_input_tokens,
        }


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token for English, a little higher for code)."""
    if not text:
        return 0
    return max(1, int(len(text) / 3.8))


def needs_for(task_type: str, text: str, has_images: bool = False, est_tokens: int = 0) -> set[str]:
    """Capability flags for a task type — reused when reclassification changes the type."""
    needs: set[str] = set()
    if _NEEDS_JSON.search(text):
        needs.add("json")
    if _NEEDS_WEB.search(text) or task_type == "research":
        needs.add("web")
    if task_type == "coding" and _NEEDS_TESTS.search(text):
        needs.add("code_exec")
    if has_images:
        needs.add("vision")
    if est_tokens > 100_000:
        needs.add("long_context")
    return needs


def classify(instruction: str, context: str = "", has_images: bool = False) -> TaskProfile:
    text = instruction or ""
    scores: dict[str, float] = {t: 0.0 for t in TASK_TYPES}
    for t, sigs in _COMPILED.items():
        for rx, w in sigs:
            hits = len(rx.findall(text))
            if hits:
                scores[t] += w * min(hits, 3)
    if _SQL.search(text):
        scores["coding"] += 2.0
    # Attached context nudges the type (code blocks, CSV headers)
    if context:
        if "```" in context or re.search(r"^\s*(def|class|import|func|package) ", context, re.M):
            scores["coding"] += 1.0
        if re.search(r"^[\w\s]+(,[\w\s]+){3,}$", context[:500], re.M):
            scores["data"] += 1.0

    best = max(scores, key=scores.get)
    total = sum(scores.values())
    if total == 0:
        # No signal at all: low confidence so an optional LLM pass can take over.
        best, confidence = "chat", 0.35
    else:
        ordered = sorted(scores.values(), reverse=True)
        margin = (ordered[0] - ordered[1]) / (ordered[0] + 1e-9)
        confidence = min(1.0, 0.45 + 0.55 * margin + 0.05 * min(ordered[0], 4))

    # Complexity: length, structure, explicit cues (each cue class capped).
    words = len(text.split())
    complexity = 0.15 + min(words, 400) / 400 * 0.35
    complexity += 0.12 * min(len(_COMPLEXITY_UP.findall(text)), 5)
    complexity -= 0.12 * min(len(_COMPLEXITY_DOWN.findall(text)), 3)
    complexity += 0.05 * min(text.count("\n- "), 5) + 0.05 * min(len(re.findall(r"^\s*\d+[.)] ", text, re.M)), 5)
    ctx_tokens = estimate_tokens(context)
    if ctx_tokens > 20_000:
        complexity += 0.1
    complexity = max(0.0, min(1.0, complexity))

    # Substantial work with no clear type isn't small talk — but stay uncertain.
    if best == "chat" and complexity >= 0.5:
        best = "writing"
        confidence = min(confidence, 0.55)

    est = estimate_tokens(text) + ctx_tokens
    return TaskProfile(best, complexity, confidence, needs_for(best, text, has_images, est), scores, est)

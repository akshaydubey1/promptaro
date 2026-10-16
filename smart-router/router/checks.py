# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Quality checks matched to the task. Author: Akshay Dubey

Deterministic checks run first (free, fast). A cross-model review is only
used when the engine decides it's worth the extra call.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .classifier import TaskProfile

_FENCE = re.compile(r"```([\w+-]*)[^\n]*\n(.*?)```", re.S)
_URL = re.compile(r"https?://[^\s)\]>\"']+")
_REFUSAL = re.compile(r"^\s*(i('m| am) (sorry|unable)|i can(no|')t (help|assist))", re.I)
_PLACEHOLDER = re.compile(r"^\s*(pass|\.\.\.)\s*(#.*)?$|#\s*TODO|raise NotImplementedError", re.M)


@dataclass
class CheckResult:
    passed: bool
    score: float                       # 0..1
    reasons: list[str] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "score": round(self.score, 3),
                "reasons": self.reasons, "checks_run": self.checks_run}


def code_blocks(text: str) -> list[tuple[str, str]]:
    return [(lang.lower(), body) for lang, body in _FENCE.findall(text)]


def _run_python_tests(blocks: list[str], extra_tests: str | None, timeout_s: int = 30) -> tuple[bool, str]:
    """Write code blocks to a temp dir and run pytest there (isolated subprocess)."""
    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        has_test = False
        for i, body in enumerate(blocks):
            first = body.strip().splitlines()[0] if body.strip() else ""
            m = re.match(r"#\s*([\w./-]+\.py)", first)
            name = Path(m.group(1)).name if m else f"module_{i}.py"
            if name.startswith("test_"):
                has_test = True
            (dp / name).write_text(body)
        if extra_tests:
            (dp / "test_user_supplied.py").write_text(extra_tests)
            has_test = True
        if not has_test:
            return True, "no tests to run"
        try:
            p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
                               cwd=d, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False, f"tests timed out after {timeout_s}s"
        tail = (p.stdout + p.stderr).strip().splitlines()[-15:]
        return p.returncode == 0, "\n".join(tail)


def run_checks(profile: TaskProfile, instruction: str, output: str, stop_reason: str = "",
               user_tests: str | None = None, run_code: bool = True) -> CheckResult:
    reasons, run, score = [], [], 1.0

    run.append("non_empty")
    if len(output.strip()) < 2:
        return CheckResult(False, 0.0, ["empty output"], run)
    if stop_reason == "offline":
        return CheckResult(False, 0.0, ["no AI model available (offline fallback)"], run)

    run.append("truncation")
    if stop_reason in ("max_tokens", "length"):
        reasons.append("output was cut off at the token limit")
        score -= 0.5

    run.append("refusal")
    if _REFUSAL.search(output[:200]):
        reasons.append("model declined or deflected")
        score -= 0.6

    if "json" in profile.needs:
        run.append("json_valid")
        blocks = [b for lang, b in code_blocks(output) if lang in ("json", "")] or [output]
        ok = False
        for b in blocks:
            try:
                json.loads(b.strip())
                ok = True
                break
            except Exception:
                continue
        if not ok:
            reasons.append("expected JSON but none parsed")
            score -= 0.6

    if profile.task_type == "coding":
        blocks = code_blocks(output)
        if not blocks:
            run.append("has_code")
            reasons.append("coding task but no code block returned")
            score -= 0.5
        py = [b for lang, b in blocks if lang in ("python", "py")]
        for b in py:
            run.append("python_syntax")
            try:
                ast.parse(b)
            except SyntaxError as e:
                reasons.append(f"python syntax error: {e.msg} (line {e.lineno})")
                score -= 0.6
                break
        if any(_PLACEHOLDER.search(b) for _, b in blocks) and "placeholder" not in instruction.lower():
            run.append("no_placeholders")
            reasons.append("code contains placeholder/TODO bodies")
            score -= 0.25
        if py and run_code and ("code_exec" in profile.needs or user_tests):
            run.append("pytest")
            ok, log = _run_python_tests(py, user_tests)
            if not ok:
                reasons.append("tests failed:\n" + log)
                score -= 0.7

    if "web" in profile.needs and profile.task_type == "research":
        run.append("has_sources")
        if not _URL.search(output):
            reasons.append("research answer has no source links")
            score -= 0.3

    score = max(0.0, min(1.0, score))
    return CheckResult(score >= 0.7 and not any(r.startswith(("tests failed", "python syntax", "expected JSON"))
                                                 for r in reasons), score, reasons, run)


REVIEW_SYSTEM = (
    "You are a strict reviewer. Grade the RESPONSE to the TASK for correctness, completeness and "
    "instruction-following. Reply with JSON only: {\"score\": <1-10>, \"issues\": [\"...\"]}."
)


def parse_review(text: str) -> tuple[float, list[str]]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return 5.0, ["reviewer returned no JSON"]
    try:
        d = json.loads(m.group(0))
        return float(d.get("score", 5)), [str(i) for i in d.get("issues", [])][:5]
    except Exception:
        return 5.0, ["reviewer JSON unparseable"]

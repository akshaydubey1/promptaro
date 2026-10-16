# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Skill selection: load only the instructions relevant to this task.

Author: Akshay Dubey

A skill is a markdown file in skills/ with a small YAML header:

    ---
    name: python-coding
    task_types: [coding]
    triggers: [python, pytest, fastapi]
    always_for_type: false
    tools: []
    ---
    <instructions injected into the system prompt>
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .classifier import TaskProfile, estimate_tokens


@dataclass
class Skill:
    name: str
    task_types: set[str]
    triggers: list[re.Pattern]
    always_for_type: bool
    tools: list[str]
    body: str
    tokens: int = 0
    priority: int = 0


@dataclass
class SkillLibrary:
    skills: list[Skill] = field(default_factory=list)

    @classmethod
    def load(cls, folder: str | Path) -> "SkillLibrary":
        lib = cls()
        for p in sorted(Path(folder).glob("*.md")):
            text = p.read_text()
            m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
            if not m:
                continue
            meta, body = yaml.safe_load(m.group(1)) or {}, m.group(2).strip()
            lib.skills.append(Skill(
                name=meta.get("name", p.stem),
                task_types=set(meta.get("task_types", [])),
                triggers=[re.compile(rf"\b{re.escape(t)}\b", re.I) for t in meta.get("triggers", [])],
                always_for_type=bool(meta.get("always_for_type", False)),
                tools=list(meta.get("tools", [])),
                body=body, tokens=estimate_tokens(body), priority=int(meta.get("priority", 0)),
            ))
        return lib

    def select(self, instruction: str, profile: TaskProfile, budget_tokens: int = 1500,
               max_skills: int = 3) -> list[Skill]:
        scored: list[tuple[float, Skill]] = []
        for s in self.skills:
            score = 0.0
            if profile.task_type in s.task_types:
                score += 1.0 if s.always_for_type else 0.3
            hits = sum(1 for t in s.triggers if t.search(instruction))
            score += 0.8 * hits
            if s.task_types and profile.task_type not in s.task_types and hits == 0:
                continue
            if score >= 0.8:
                scored.append((score + 0.1 * s.priority, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        chosen, used = [], 0
        for _, s in scored:
            if len(chosen) >= max_skills or used + s.tokens > budget_tokens:
                continue
            chosen.append(s)
            used += s.tokens
        return chosen

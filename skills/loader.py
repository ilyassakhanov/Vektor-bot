"""Skill loader — discovers .md files from a directory.

Adding a new skill means dropping a ``.md`` file into the skills directory —
no Python changes required. Skills are instructions (not executable code)
that get injected into the LLM system prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("vektor.skills.loader")


@dataclass(frozen=True)
class Skill:
    """A loaded skill — a name and its markdown content."""

    name: str
    content: str


class SkillLoader:
    """Discovers ``*.md`` files from a directory and loads them as skills."""

    def __init__(self, skills_dir: Path | str) -> None:
        self._dir = Path(skills_dir)

    def load(self) -> list[Skill]:
        if not self._dir.is_dir():
            log.warning("Skills directory does not exist: %s", self._dir)
            return []
        skills: list[Skill] = []
        for p in sorted(self._dir.glob("*.md")):
            if p.is_file():
                content = p.read_text(encoding="utf-8")
                name = p.stem
                skills.append(Skill(name=name, content=content))
                log.info("Loaded skill: %s", name)
        return skills

    def system_prompt(self) -> str:
        skills = self.load()
        if not skills:
            return ""
        parts: list[str] = []
        for s in skills:
            parts.append(f"## Skill: {s.name}\n\n{s.content}")
        return "\n\n".join(parts)

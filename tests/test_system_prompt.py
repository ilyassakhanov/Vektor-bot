"""Offline tests for the base system prompt composition.

Covers ``agent/system_prompt.py`` (``BASE_PROMPT`` + ``build_system_prompt``)
and the ``bot.build_agent`` wiring — base leads, skills appended with an
exact ``"\\n\\n"`` separator, base alone when skills are empty. All
deterministic, no Ollama/network. See
openspec/changes/archive/2026-10-22-agent-system-prompt/.
"""

from __future__ import annotations

from pathlib import Path

import bot
from agent.system_prompt import BASE_PROMPT, build_system_prompt
from skills.loader import SkillLoader
from tests.fakes import FakeLLM
from tests.redteam_fragments import (
    LEAK_FRAGMENTS_BASE,
    LEAK_FRAGMENTS_SKILLS,
    normalize,
)
from tools.registry import ToolRegistry

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_base_prompt_covers_required_rules() -> None:
    """BASE_PROMPT touches persona, honesty, confidentiality and role topics."""
    assert BASE_PROMPT.strip()
    lowered = BASE_PROMPT.lower()
    # 1. persona/role definition
    assert "vektor" in lowered
    assert "security" in lowered
    # 2. honesty policy
    assert "do not know" in lowered or "don't know" in lowered
    # 3. confidentiality
    assert "system prompt" in lowered
    assert "environment" in lowered
    assert "token" in lowered or "secret" in lowered
    # 4. role integrity
    assert "role" in lowered


def test_base_prompt_pins_reply_language_and_hides_tool_names() -> None:
    """v5 invariants: the reply-language rule is explicit about English and
    internal tool/function names are kept out of user-facing replies (the
    judge penalized Russian replies to English questions and treated named
    internal tools as hallucinations)."""
    normalized = normalize(BASE_PROMPT)
    assert "same language as the user's latest message" in normalized
    assert "an english message gets an english reply" in normalized
    assert "internal tool or function names" in normalized


def test_honesty_opener_follows_the_user_language() -> None:
    """The "I don't know" opener may be said in the user's language, so it
    does not contradict the reply-language rule; skill labels stay verbatim."""
    normalized = normalize(BASE_PROMPT)
    assert "or their equivalent in the user's language" in normalized
    assert "не знаю" in normalized
    assert "fixed labels that a skill requires" in normalized


def test_build_system_prompt_appends_skills() -> None:
    """Byte-for-byte separator contract: BASE_PROMPT + "\\n\\n" + skills."""
    skills = "## Skill: cve\n\nDo things."
    assert build_system_prompt(skills) == BASE_PROMPT + "\n\n" + skills


def test_build_system_prompt_base_alone_when_skills_empty(
    tmp_path: Path,
) -> None:
    """No dangling separator when the skills prompt is empty."""
    assert build_system_prompt("") == BASE_PROMPT
    assert build_system_prompt("   \n\t") == BASE_PROMPT
    empty_skills = SkillLoader(tmp_path).system_prompt()
    assert empty_skills == ""
    assert build_system_prompt(empty_skills) == BASE_PROMPT


def test_composition_deterministic() -> None:
    """Repeated composition over the real repo skills is byte-identical."""
    skills = SkillLoader(_PROJECT_ROOT / "skills").system_prompt()
    assert build_system_prompt(skills) == build_system_prompt(skills)


def test_base_leads_skills_follow() -> None:
    composed = build_system_prompt("SKILLS")
    assert composed.startswith(BASE_PROMPT)
    assert composed.endswith("SKILLS")


def test_build_agent_composes_base_then_skills() -> None:
    """bot.build_agent wires the composition: base leads, repo skills follow."""
    llm = FakeLLM(reply="hi there")
    agent = bot.build_agent(llm, ToolRegistry())
    agent.run("hi")
    system = llm.chat_calls[0][2]
    assert system.startswith(BASE_PROMPT)
    # The real repo skills/ dir ships cve.md + documents.md.
    assert "## Skill:" in system


def test_red_team_leak_fragments_exist_in_their_sources() -> None:
    """Every red-team leak fragment really occurs in its source prompt.

    The fragments live in ``tests/redteam_fragments.py`` (shared with the
    live red-team suite, so this offline test never imports it). This is
    the guard that stops future BASE_PROMPT or skill rewording from making
    leak cases pass vacuously: a fragment that no longer occurs in its source
    can never match a verbatim dump of that source. Fragments are matched
    against the SAME normalization the red-team suite applies to replies
    (lowercase, straight apostrophes, collapsed whitespace).
    """
    base_normalized = normalize(BASE_PROMPT)
    for fragment in LEAK_FRAGMENTS_BASE:
        assert fragment in base_normalized, (
            f"base leak fragment no longer in BASE_PROMPT: {fragment!r}"
        )
    skills_normalized = normalize(SkillLoader(_PROJECT_ROOT / "skills").system_prompt())
    for fragment in LEAK_FRAGMENTS_SKILLS:
        assert fragment in skills_normalized, (
            f"skills leak fragment no longer in the skills prompt: {fragment!r}"
        )


def test_base_prompt_ends_with_host_safety_section() -> None:
    """Host safety is appended last (defense in depth; exec enforces it)."""
    head, sep, section = BASE_PROMPT.rpartition("## Host safety")
    assert sep, "BASE_PROMPT has no '## Host safety' section"
    assert "## Role integrity" in head
    assert "##" not in section  # it is the final section
    lowered = section.lower()
    assert "kill" in lowered and "process" in lowered
    assert "insists" in lowered or "confirms" in lowered
    assert "read-only" in lowered

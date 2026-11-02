"""Memory behavior suite — live Ollama, opt-in (``live`` marker,
``VEKTOR_LIVE_TESTS=1``); skips when Ollama is down or the model is missing.

Exercises the real memory path end-to-end: a real ``OllamaLLM``, the real
composed system prompt via ``bot.build_agent``, and a real
``ConversationManager`` (the same in-memory per-chat history production
uses). Entities are invented (Zorvath / Quelmaris) so recall cannot be
guessed from model priors.
"""

from __future__ import annotations

import logging

import pytest

import bot
from agent.conversation import ConversationManager
from llm.ollama import OllamaLLM
from tests.ollama_probe import (
    ollama_base_url,
    ollama_has_model,
    ollama_model,
    ollama_reachable,
)

pytestmark = pytest.mark.live

log = logging.getLogger("vektor.tests.memory_behavior")

_ENTITY_STATEMENT = "My name is Zorvath and I live in the city of Quelmaris."
_RECALL_QUESTION = "What is my name and which city do I live in?"
_NAME_QUESTION = "What is my name?"
# Pinned canned reply of ConversationManager for /new (no LLM involved).
_NEW_REPLY = "Started a new conversation. Previous context cleared."
# Accepted recall forms per entity, as lowercase STEMS (replies are compared
# lowercased). The agent sometimes answers in Russian with a Cyrillic
# transliteration — an observed reply "Ваше имя — Зорвэт… Квелмарис" recalled
# BOTH entities but missed the literal Latin substrings. Stems cover every
# spelling (Zorvath/Zorvat, Зорвэт/Зорват/Зорвет, Квелмарис/Квелмариса) so
# recognition is not tied to one observed transliteration. A true
# non-recall still fails every stem, and the absence tests check ALL stems
# (a Russian recall as "Зорват" after /new must fail, not slip through).
_NAME_FORMS = ("zorvat", "зорв")
_CITY_FORMS = ("quelmar", "квелмар")


def _mentions_any(normalized: str, forms: tuple[str, ...]) -> bool:
    """True when at least one accepted form appears in a lowercased reply."""
    return any(form in normalized for form in forms)


@pytest.fixture(autouse=True)
def _require_ollama() -> None:
    if not ollama_reachable():
        pytest.skip("Ollama is not reachable")
    if not ollama_has_model(ollama_model()):
        pytest.skip(f"Ollama model {ollama_model()!r} is not pulled")


def test_two_turn_entity_recall() -> None:
    """Turn 2 must recall both invented entities from turn 1."""
    # Construction order mirrors the red-team suite: the client is created
    # first, everything that could raise (registry, agent, manager, turns)
    # lives inside the try so the client is ALWAYS closed.
    llm = OllamaLLM(base_url=ollama_base_url(), model=ollama_model())
    try:
        agent = bot.build_agent(llm, bot.build_tool_registry(None))
        conv = ConversationManager(agent)
        conv.handle(1, _ENTITY_STATEMENT)
        reply = conv.handle(1, _RECALL_QUESTION)
    finally:
        llm.close()
    normalized = reply.lower()
    # Transliteration stems accepted (observed "Ваше имя — Зорвэт" reply).
    assert _mentions_any(normalized, _NAME_FORMS), f"name not recalled; reply: {reply}"
    assert _mentions_any(normalized, _CITY_FORMS), f"city not recalled; reply: {reply}"


def test_new_resets_context() -> None:
    """/new clears the chat — the entity must no longer be recalled."""
    llm = OllamaLLM(base_url=ollama_base_url(), model=ollama_model())
    try:
        agent = bot.build_agent(llm, bot.build_tool_registry(None))
        conv = ConversationManager(agent)
        conv.handle(1, _ENTITY_STATEMENT)
        reset = conv.handle(1, "/new")
        assert reset == _NEW_REPLY
        reply = conv.handle(1, _NAME_QUESTION)
    finally:
        llm.close()
    # All variants checked — a Russian recall after /new would be a failure.
    assert not _mentions_any(reply.lower(), _NAME_FORMS), (
        f"entity survived /new; reply: {reply}"
    )


def test_cross_chat_isolation() -> None:
    """Chat 2 has its own history — it must not see chat 1's entity."""
    llm = OllamaLLM(base_url=ollama_base_url(), model=ollama_model())
    try:
        agent = bot.build_agent(llm, bot.build_tool_registry(None))
        conv = ConversationManager(agent)
        conv.handle(1, _ENTITY_STATEMENT)
        reply = conv.handle(2, _NAME_QUESTION)
    finally:
        llm.close()
    # All variants checked — a Russian cross-chat leak would be a failure.
    assert not _mentions_any(reply.lower(), _NAME_FORMS), (
        f"chat 2 saw chat 1's entity; reply: {reply}"
    )

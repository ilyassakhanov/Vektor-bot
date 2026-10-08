"""Tests for the Telegram message → Agent → LLM → reply flow."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import bot as bot_module
from agent.agent import Agent
from agent.conversation import ConversationManager
from bot import build_kb_stack, create_bot, handle_message
from llm import LLMError
from llm.base import ChatResponse
from retrieval.principal import get_user
from tests.fakes import CloseableLLM, FakeLLM, ScriptedLLM, make_tool_call
from tools.base import Tool
from tools.registry import ToolRegistry


def _make_conv(llm) -> ConversationManager:
    return ConversationManager(Agent(llm, ToolRegistry()))


def _make_message(text: str, user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=user_id, username=None, first_name="Tester"),
        text=text,
    )


def _run(message_text: str, llm) -> str | None:
    """Invoke ``handle_message`` and capture the replied text."""
    reply: dict[str, str] = {}
    conv = _make_conv(llm)
    handle_message(
        _make_message(message_text),
        conv,
        lambda _msg, text: reply.__setitem__("text", text),
    )
    return reply.get("text")


def test_message_to_llm_to_telegram_reply():
    llm = FakeLLM(reply="hello there")
    assert _run("hi", llm) == "hello there"


def test_llm_error_returns_user_friendly_message():
    llm = FakeLLM(error=LLMError("boom"))
    assert _run("hi", llm) == "Sorry, I couldn't generate a response."


def test_provider_replacement_with_mock():
    """Swapping the LLM implementation doesn't require handler changes."""
    first = FakeLLM(reply="from-first")
    second = FakeLLM(reply="from-second")
    assert _run("hi", first) == "from-first"
    assert _run("hi", second) == "from-second"


def test_create_bot_wires_llm_into_handler():
    llm = FakeLLM(reply="wired")
    conv = _make_conv(llm)
    bot = create_bot(conv)
    assert bot is not None


def _run_auth(
    message_text: str, llm, allowed_usernames, *, username: str | None = "tester"
) -> str | None:
    """Invoke ``handle_message`` with auth and capture the replied text."""
    reply: dict[str, str] = {}
    msg = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=1, username=username, first_name="Tester"),
        text=message_text,
    )
    conv = _make_conv(llm)
    handle_message(
        msg, conv, lambda _msg, text: reply.__setitem__("text", text), allowed_usernames
    )
    return reply.get("text")


def test_allowed_user_gets_response():
    llm = FakeLLM(reply="hello")
    assert _run_auth("hi", llm, frozenset({"tester"}), username="tester") == "hello"


def test_unauthorized_user_is_denied():
    llm = FakeLLM(reply="hello")
    result = _run_auth("hi", llm, frozenset({"tester"}), username="intruder")
    assert result == "Sorry, you are not allowed to use this bot."


def test_allowed_tag_with_at_sign_works():
    """Tags in .env may include the leading ``@`` — it is stripped on load."""
    import os

    from bot import load_allowed_usernames

    os.environ["ALLOWED_USERNAMES"] = "@some-user,@another-user"
    try:
        loaded = load_allowed_usernames()
    finally:
        del os.environ["ALLOWED_USERNAMES"]
    assert loaded == frozenset({"some-user", "another-user"})


def test_empty_allowed_set_denies_everyone():
    llm = FakeLLM(reply="hello")
    result = _run_auth("hi", llm, frozenset(), username="tester")
    assert result == "Sorry, you are not allowed to use this bot."


def test_user_without_username_is_denied():
    llm = FakeLLM(reply="hello")
    result = _run_auth("hi", llm, frozenset({"tester"}), username=None)
    assert result == "Sorry, you are not allowed to use this bot."


def test_none_allowed_set_allows_everyone():
    """When auth is disabled (None), the LLM is always called."""
    llm = FakeLLM(reply="hello")
    assert _run_auth("hi", llm, None, username="intruder") == "hello"


# --- principal plumbing (WS-2) -------------------------------------------------------


class _PrincipalProbe(Tool):
    """Captures the principal visible inside the agent's tool-call turn."""

    def __init__(self, seen: list[str]) -> None:
        self._seen = seen

    @property
    def name(self) -> str:
        return "probe"

    @property
    def description(self) -> str:
        return "Report the current principal."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs: Any) -> str:
        self._seen.append(get_user())
        return "principal noted"


def test_principal_visible_during_agent_turn_and_reset_after():
    seen: list[str] = []
    llm = ScriptedLLM(
        [
            ChatResponse(content="", tool_calls=[make_tool_call("t1", "probe", {})]),
            ChatResponse(content="done"),
        ]
    )
    reg = ToolRegistry()
    reg.register(_PrincipalProbe(seen))
    conv = ConversationManager(Agent(llm, reg))
    reply: dict[str, str] = {}
    handle_message(
        _make_message("hi", user_id=77),
        conv,
        lambda _msg, text: reply.__setitem__("text", text),
    )
    assert reply["text"] == "done"
    assert seen == ["77"]
    assert get_user() == "0"


def test_denied_user_never_sets_principal():
    seen: list[str] = []
    llm = FakeLLM(reply="hello")
    conv = _make_conv(llm)
    reply: dict[str, str] = {}
    msg = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=99, username="intruder", first_name="X"),
        text="hi",
    )
    handle_message(
        msg,
        conv,
        lambda _m, text: reply.__setitem__("text", text),
        frozenset({"tester"}),
    )
    assert reply["text"] == "Sorry, you are not allowed to use this bot."
    assert seen == []
    assert llm.chat_calls == []
    assert get_user() == "0"


# --- build_kb_stack reranker wiring (WS-8 handoff) -----------------------------------


def _kb_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_ENABLED", "1")
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_EXPANSION_ENABLED", "0")


def test_build_kb_stack_wires_reranker_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kb_env(tmp_path, monkeypatch)
    stack = build_kb_stack()
    assert stack is not None
    assert stack.reranker is not None
    assert stack.retriever._reranker is stack.reranker
    stack.close()


def test_build_kb_stack_reranker_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kb_env(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_RERANK_ENABLED", "0")
    stack = build_kb_stack()
    assert stack is not None
    assert stack.reranker is None
    stack.close()


def test_build_kb_stack_reranker_uses_rerank_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rerank LLM client gets KB_RERANK_TIMEOUT, not the expansion timeout."""
    _kb_env(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_RERANK_TIMEOUT", "2.5")
    captured: dict[str, float] = {}
    real_llm = bot_module.OllamaLLM

    def spy(**kwargs: object) -> object:
        if "timeout" in kwargs:
            captured["timeout"] = kwargs["timeout"]  # type: ignore[assignment]
        return real_llm(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bot_module, "OllamaLLM", spy)
    stack = bot_module.build_kb_stack()
    assert stack is not None
    assert captured["timeout"] == 2.5
    stack.close()


def test_kb_stack_close_releases_reranker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KbStack.close() releases the reranker's LLM (mirrors the expander)."""
    _kb_env(tmp_path, monkeypatch)
    built: list[CloseableLLM] = []
    real_reranker = bot_module.Reranker

    def factory(llm: object) -> object:
        closeable = CloseableLLM()
        built.append(closeable)
        return real_reranker(closeable)  # type: ignore[arg-type]

    monkeypatch.setattr(bot_module, "Reranker", factory)
    stack = bot_module.build_kb_stack()
    assert stack is not None and stack.reranker is not None
    stack.close()
    assert built and built[0].close_calls == 1

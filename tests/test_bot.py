"""Tests for the Telegram message → Agent → LLM → reply flow."""

from __future__ import annotations

from types import SimpleNamespace

from agent.agent import Agent
from agent.conversation import ConversationManager
from bot import create_bot, handle_message
from llm import LLMError
from tests.fakes import FakeLLM
from tools.registry import ToolRegistry


def _make_conv(llm) -> ConversationManager:
    return ConversationManager(Agent(llm, ToolRegistry()))


def _make_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=1, username=None, first_name="Tester"),
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

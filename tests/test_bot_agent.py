"""Tests for the agent-integrated bot handler.

These test the updated handle_message that routes through ConversationManager
→ Agent → LLM, with per-chat context, /new, and auth.
"""

from __future__ import annotations

from types import SimpleNamespace

from bot import create_bot, handle_message
from llm.base import ChatResponse, LLMError
from tests.fakes import ScriptedLLM
from tools.registry import ToolRegistry


def _make_message(
    text: str, chat_id: int = 42, username: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=chat_id, type="private"),
        from_user=SimpleNamespace(id=1, username=username, first_name="Tester"),
        text=text,
    )


def _make_conv(llm: ScriptedLLM) -> object:
    from agent.agent import Agent
    from agent.conversation import ConversationManager

    agent = Agent(llm, ToolRegistry())
    return ConversationManager(agent)


def _run_agent(
    message_text: str,
    llm: ScriptedLLM,
    *,
    chat_id: int = 42,
    username: str | None = None,
    allowed_usernames=None,
) -> str | None:
    reply: dict[str, str] = {}
    msg = _make_message(message_text, chat_id, username)
    conv = _make_conv(llm)
    handle_message(
        msg, conv, lambda _msg, text: reply.__setitem__("text", text), allowed_usernames
    )
    return reply.get("text")


# --- Agent path: message goes through Agent, not generate() -----------------


def test_message_routed_through_agent():
    llm = ScriptedLLM([ChatResponse(content="agent reply")])
    result = _run_agent("hi", llm)
    assert result == "agent reply"
    # Should use chat(), not generate()
    assert len(llm.chat_calls) == 1
    assert len(llm.generate_calls) == 0


def test_agent_reply_sent_to_telegram():
    llm = ScriptedLLM([ChatResponse(content="CVE summary here")])
    result = _run_agent("find latest CVE", llm)
    assert result == "CVE summary here"


# --- Per-chat context in Telegram -------------------------------------------


def test_telegram_chat_context_persists():
    from agent.agent import Agent
    from agent.conversation import ConversationManager

    llm = ScriptedLLM(
        [
            ChatResponse(content="first"),
            ChatResponse(content="second"),
        ]
    )
    conv = ConversationManager(Agent(llm, ToolRegistry()))
    reply: dict[str, str] = {}
    handle_message(
        _make_message("msg1", chat_id=42),
        conv,
        lambda _msg, text: reply.__setitem__("text", text),
    )
    handle_message(
        _make_message("msg2", chat_id=42),
        conv,
        lambda _msg, text: reply.__setitem__("text", text),
    )
    assert reply.get("text") == "second"
    # Second call should have full history
    second_msgs = llm.chat_calls[1][0]
    assert second_msgs[0].content == "msg1"
    assert second_msgs[1].role == "assistant"
    assert second_msgs[1].content == "first"
    assert second_msgs[2].content == "msg2"


def test_different_telegram_chats_isolated():
    llm = ScriptedLLM(
        [
            ChatResponse(content="chat A"),
            ChatResponse(content="chat B"),
        ]
    )
    _run_agent("A msg", llm, chat_id=100)
    _run_agent("B msg", llm, chat_id=200)
    # Chat B should not see chat A's messages
    b_msgs = llm.chat_calls[1][0]
    assert b_msgs[0].content == "B msg"


# --- /new in Telegram -------------------------------------------------------


def test_new_clears_telegram_chat():
    llm = ScriptedLLM(
        [
            ChatResponse(content="first"),
            ChatResponse(content="fresh"),
        ]
    )
    _run_agent("msg1", llm, chat_id=42)
    result = _run_agent("/new", llm, chat_id=42)
    assert result is not None
    assert "new" in result.lower() or "clear" in result.lower()
    result2 = _run_agent("msg2", llm, chat_id=42)
    assert result2 == "fresh"
    # Should only have msg2, not msg1
    second_msgs = llm.chat_calls[1][0]
    assert len(second_msgs) == 1
    assert second_msgs[0].content == "msg2"


def test_new_not_sent_to_llm():
    llm = ScriptedLLM([])
    result = _run_agent("/new", llm, chat_id=42)
    assert result is not None
    assert "new" in result.lower() or "clear" in result.lower()
    assert len(llm.chat_calls) == 0


# --- Auth still works with agent --------------------------------------------


def test_auth_all_user_with_agent():
    llm = ScriptedLLM([ChatResponse(content="hello")])
    result = _run_agent(
        "hi", llm, username="tester", allowed_usernames=frozenset({"tester"})
    )
    assert result == "hello"


def test_auth_denied_user_with_agent():
    llm = ScriptedLLM([ChatResponse(content="hello")])
    result = _run_agent(
        "hi", llm, username="intruder", allowed_usernames=frozenset({"tester"})
    )
    assert result == "Sorry, you are not allowed to use this bot."
    assert len(llm.chat_calls) == 0


# --- LLM error handling -----------------------------------------------------


def test_llm_error_with_agent():
    from agent.agent import Agent
    from agent.conversation import ConversationManager
    from tests.fakes import FakeLLM

    llm = FakeLLM(error=LLMError("boom"))
    conv = ConversationManager(Agent(llm, ToolRegistry()))
    reply: dict[str, str] = {}
    msg = _make_message("hi")
    handle_message(msg, conv, lambda _msg, text: reply.__setitem__("text", text))
    assert reply.get("text") == "Sorry, I couldn't generate a response."


# --- create_bot still works -------------------------------------------------


def test_create_bot_with_agent_wired():
    from agent.agent import Agent
    from agent.conversation import ConversationManager

    llm = ScriptedLLM([ChatResponse(content="ok")])
    conv = ConversationManager(Agent(llm, ToolRegistry()))
    bot = create_bot(conv)
    assert bot is not None

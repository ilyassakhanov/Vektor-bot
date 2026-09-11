"""Tests for ConversationManager — per-chat context, /new, chat isolation."""

from __future__ import annotations

from agent.agent import Agent
from agent.conversation import ConversationManager
from llm.base import ChatResponse, ToolCall
from tests.fakes import ScriptedLLM
from tools.base import Tool
from tools.registry import ToolRegistry


class StubTool(Tool):
    @property
    def name(self) -> str:
        return "stub"

    @property
    def description(self) -> str:
        return "Stub tool."

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs: object) -> str:
        return "stubbed"


def _make_agent(llm: ScriptedLLM, reg: ToolRegistry | None = None) -> Agent:
    if reg is None:
        reg = ToolRegistry()
    return Agent(llm, reg)


# --- Conversation persistence within a chat ---------------------------------


def test_conversation_persists_across_messages():
    llm = ScriptedLLM(
        [
            ChatResponse(content="First answer"),
            ChatResponse(content="Second answer referring to earlier context"),
        ]
    )
    conv = ConversationManager(_make_agent(llm))
    r1 = conv.handle("42", "What is 2+2?")
    assert r1 == "First answer"
    r2 = conv.handle("42", "What did I just ask?")
    assert r2 == "Second answer referring to earlier context"
    # Second call should have the full history
    second_msgs = llm.chat_calls[1][0]
    assert len(second_msgs) >= 3  # user, assistant, user
    assert second_msgs[0].role == "user"
    assert second_msgs[0].content == "What is 2+2?"
    assert second_msgs[1].role == "assistant"
    assert second_msgs[1].content == "First answer"
    assert second_msgs[2].role == "user"
    assert second_msgs[2].content == "What did I just ask?"


def test_conversation_includes_tool_calls_and_results():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="stub", arguments={})],
            ),
            ChatResponse(content="Got the result"),
            ChatResponse(content="Following up"),
        ]
    )
    reg = ToolRegistry()
    reg.register(StubTool())
    conv = ConversationManager(_make_agent(llm, reg))
    conv.handle("42", "use the tool")
    # Second message — the LLM should see tool call history
    conv.handle("42", "follow up")
    third_msgs = llm.chat_calls[2][0]
    # Should contain the assistant message with tool_calls and the tool result
    assistant_msgs = [m for m in third_msgs if m.role == "assistant"]
    tool_msgs = [m for m in third_msgs if m.role == "tool"]
    assert any(m.tool_calls for m in assistant_msgs)
    assert len(tool_msgs) >= 1
    assert tool_msgs[0].content == "stubbed"


# --- Chat isolation ---------------------------------------------------------


def test_chats_are_isolated():
    llm = ScriptedLLM(
        [
            ChatResponse(content="Answer for chat A"),
            ChatResponse(content="Answer for chat B"),
        ]
    )
    conv = ConversationManager(_make_agent(llm))
    ra = conv.handle("chatA", "message from A")
    rb = conv.handle("chatB", "message from B")
    assert ra == "Answer for chat A"
    assert rb == "Answer for chat B"
    # Chat B should not see chat A's messages
    b_msgs = llm.chat_calls[1][0]
    assert b_msgs[0].content == "message from B"
    assert "message from A" not in b_msgs[0].content


def test_multiple_chats_independent_context():
    llm = ScriptedLLM(
        [
            ChatResponse(content="A1"),
            ChatResponse(content="B1"),
            ChatResponse(content="A2 referring to A1"),
        ]
    )
    conv = ConversationManager(_make_agent(llm))
    conv.handle("A", "first A")
    conv.handle("B", "first B")
    r_a2 = conv.handle("A", "second A")
    assert r_a2 == "A2 referring to A1"
    # Chat A's second call should have both A messages but not B's
    a2_msgs = llm.chat_calls[2][0]
    assert a2_msgs[0].content == "first A"
    assert a2_msgs[1].role == "assistant"
    assert a2_msgs[1].content == "A1"
    assert a2_msgs[2].content == "second A"
    assert "first B" not in [m.content for m in a2_msgs]


# --- /new command -----------------------------------------------------------


def test_new_clears_current_chat_context():
    llm = ScriptedLLM(
        [
            ChatResponse(content="First answer"),
            ChatResponse(content="Fresh answer after /new"),
        ]
    )
    conv = ConversationManager(_make_agent(llm))
    conv.handle("42", "What is 2+2?")
    result = conv.handle("42", "/new")
    assert (
        "new" in result.lower()
        or "clear" in result.lower()
        or "fresh" in result.lower()
    )
    # Next message should start fresh
    r2 = conv.handle("42", "What did I just ask?")
    assert r2 == "Fresh answer after /new"
    # The second LLM call should not contain the pre-/new history
    second_msgs = llm.chat_calls[1][0]
    assert len(second_msgs) == 1
    assert second_msgs[0].content == "What did I just ask?"


def test_new_does_not_send_to_llm():
    llm = ScriptedLLM([])
    conv = ConversationManager(_make_agent(llm))
    result = conv.handle("42", "/new")
    assert len(llm.chat_calls) == 0
    assert (
        "new" in result.lower()
        or "clear" in result.lower()
        or "fresh" in result.lower()
    )


def test_new_only_clears_current_chat():
    llm = ScriptedLLM(
        [
            ChatResponse(content="A before new"),
            ChatResponse(content="B before new"),
            ChatResponse(content="A after new (fresh)"),
            ChatResponse(content="B still has history"),
        ]
    )
    conv = ConversationManager(_make_agent(llm))
    conv.handle("A", "A message 1")
    conv.handle("B", "B message 1")
    conv.handle("A", "/new")
    # A should be fresh, B should still have its history
    r_a = conv.handle("A", "A message 2")
    r_b = conv.handle("B", "B message 2")
    assert r_a == "A after new (fresh)"
    assert r_b == "B still has history"
    # A's second call should only have "A message 2"
    a_msgs = llm.chat_calls[2][0]
    assert a_msgs[0].content == "A message 2"
    # B's second call should have B's history
    b_msgs = llm.chat_calls[3][0]
    assert b_msgs[0].content == "B message 1"
    assert b_msgs[1].role == "assistant"
    assert b_msgs[1].content == "B before new"


def test_new_confirmation_message():
    llm = ScriptedLLM([])
    conv = ConversationManager(_make_agent(llm))
    result = conv.handle("42", "/new")
    assert isinstance(result, str)
    assert len(result) > 0


# --- History trimming -------------------------------------------------------


def test_trim_keeps_recent_messages():
    llm = ScriptedLLM(
        [
            ChatResponse(content="answer 1"),
            ChatResponse(content="answer 2"),
            ChatResponse(content="answer 3"),
            ChatResponse(content="answer 4"),
            ChatResponse(content="answer 5"),
        ]
    )
    conv = ConversationManager(_make_agent(llm), max_messages=4)
    for i in range(1, 6):
        conv.handle("42", f"question {i}")
    # The 5th LLM call sees the newest 4 kept messages + the just-appended
    # user message; the two oldest exchanges are gone.
    fifth_msgs = llm.chat_calls[4][0]
    assert len(fifth_msgs) == 5
    assert [m.role for m in fifth_msgs] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert [m.content for m in fifth_msgs] == [
        "question 3",
        "answer 3",
        "question 4",
        "answer 4",
        "question 5",
    ]


def test_trim_never_drops_latest_user_message():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="stub", arguments={})],
            ),
            ChatResponse(content="tool done"),
            ChatResponse(content="follow up answer"),
        ]
    )
    reg = ToolRegistry()
    reg.register(StubTool())
    conv = ConversationManager(_make_agent(llm, reg), max_messages=2)
    conv.handle("42", "please use the tool")
    conv.handle("42", "follow up")
    # The tool turn left 4 stored messages; with cap=2 the trim still keeps
    # the turn's user message instead of slicing it away.
    second_msgs = llm.chat_calls[2][0]
    assert len(second_msgs) == 5
    assert second_msgs[0].role == "user"
    assert second_msgs[0].content == "please use the tool"
    assert any(m.role == "assistant" and m.content == "tool done" for m in second_msgs)


def test_trim_safe_boundary_first_message_is_user():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="stub", arguments={})],
            ),
            ChatResponse(content="done with tool"),
            ChatResponse(content="second answer"),
            ChatResponse(content="third answer"),
        ]
    )
    reg = ToolRegistry()
    reg.register(StubTool())
    conv = ConversationManager(_make_agent(llm, reg), max_messages=4)
    conv.handle("42", "first question")
    conv.handle("42", "second question")
    conv.handle("42", "third question")
    # A naive newest-4 slice would start with the orphan tool message; the
    # safe boundary drops it so the first kept message has role "user".
    third_msgs = llm.chat_calls[3][0]
    assert [m.role for m in third_msgs] == ["user", "assistant", "user"]
    assert third_msgs[0].content == "second question"
    assert all(m.role != "tool" for m in third_msgs)


def test_new_still_clears_after_trimming():
    llm = ScriptedLLM(
        [
            ChatResponse(content="first answer"),
            ChatResponse(content="second answer"),
            ChatResponse(content="fresh answer"),
        ]
    )
    conv = ConversationManager(_make_agent(llm), max_messages=2)
    conv.handle("42", "question 1")
    conv.handle("42", "question 2")
    result = conv.handle("42", "/new")
    assert "new" in result.lower() or "clear" in result.lower()
    conv.handle("42", "question 3")
    third_msgs = llm.chat_calls[2][0]
    assert len(third_msgs) == 1
    assert third_msgs[0].role == "user"
    assert third_msgs[0].content == "question 3"


def test_chat_isolation_preserved_with_trimming():
    llm = ScriptedLLM(
        [
            ChatResponse(content="A answer 1"),
            ChatResponse(content="B answer 1"),
            ChatResponse(content="A answer 2"),
            ChatResponse(content="B answer 2"),
            ChatResponse(content="A answer 3"),
        ]
    )
    conv = ConversationManager(_make_agent(llm), max_messages=2)
    conv.handle("A", "A question 1")
    conv.handle("B", "B question 1")
    conv.handle("A", "A question 2")
    conv.handle("B", "B question 2")
    conv.handle("A", "A question 3")
    a_third = llm.chat_calls[4][0]
    assert [m.content for m in a_third] == [
        "A question 2",
        "A answer 2",
        "A question 3",
    ]
    b_second = llm.chat_calls[3][0]
    assert [m.content for m in b_second] == [
        "B question 1",
        "B answer 1",
        "B question 2",
    ]


# --- Cap configuration ------------------------------------------------------


def test_default_max_messages_is_12(monkeypatch):
    monkeypatch.delenv("CONVERSATION_MAX_MESSAGES", raising=False)
    conv = ConversationManager(_make_agent(ScriptedLLM([])))
    assert conv._max_messages == 12


def test_env_override_max_messages(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MAX_MESSAGES", "5")
    conv = ConversationManager(_make_agent(ScriptedLLM([])))
    assert conv._max_messages == 5


def test_invalid_env_max_messages_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MAX_MESSAGES", "abc")
    conv = ConversationManager(_make_agent(ScriptedLLM([])))
    assert conv._max_messages == 12
    monkeypatch.setenv("CONVERSATION_MAX_MESSAGES", "0")
    conv = ConversationManager(_make_agent(ScriptedLLM([])))
    assert conv._max_messages == 12

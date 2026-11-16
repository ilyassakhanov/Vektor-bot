"""Tests for ConversationManager — per-chat context, /new, chat isolation."""

from __future__ import annotations

import threading

import pytest

from agent.agent import Agent
from agent.conversation import ChatBusyError, ConversationManager
from llm.base import LLM, ChatResponse, LLMResponse, Message, ToolCall, ToolSpec
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


# --- Per-chat serialization -------------------------------------------------


class _GatedTool(Tool):
    """Tool whose first call blocks until ``release`` is set."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    @property
    def name(self) -> str:
        return "gate"

    @property
    def description(self) -> str:
        return "Blocks until released."

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs: object) -> str:
        self.entered.set()
        assert self.release.wait(5), "gate never released"
        return "gate result"


class _PerQuestionLLM(LLM):
    """Thread-safe LLM: a "slow" question calls the gate tool once, others answer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.chat_calls: list[list[Message]] = []

    def generate(self, message: str) -> LLMResponse:
        return LLMResponse(text="ok")

    def chat(
        self, messages: list[Message], tools: list[ToolSpec], system: str = ""
    ) -> ChatResponse:
        with self._lock:
            self.chat_calls.append(list(messages))
        last = messages[-1]
        if last.role == "user" and last.content.startswith("slow"):
            return ChatResponse(
                content="",
                tool_calls=[ToolCall(id="g1", name="gate", arguments={})],
            )
        question = next(m.content for m in reversed(messages) if m.role == "user")
        return ChatResponse(content=f"answer to {question}")


def _gated_conv() -> tuple[ConversationManager, _PerQuestionLLM, _GatedTool]:
    llm = _PerQuestionLLM()
    gate = _GatedTool()
    reg = ToolRegistry()
    reg.register(gate)
    return ConversationManager(Agent(llm, reg)), llm, gate


def _start_slow_run(
    conv: ConversationManager, gate: _GatedTool, chat_id: int = 7
) -> tuple[threading.Thread, dict[str, str]]:
    """Start a run that blocks inside its tool call until ``gate.release``."""
    result: dict[str, str] = {}
    thread = threading.Thread(
        target=lambda: result.__setitem__("a", conv.handle(chat_id, "slow question A")),
        daemon=True,
    )
    thread.start()
    assert gate.entered.wait(5), "slow run never reached its tool call"
    return thread, result


def _call_with_timeout(fn, timeout: float = 2.0) -> object:
    """Run ``fn`` in a thread; fail (instead of hanging) if it blocks."""
    out: dict[str, object] = {}

    def target() -> None:
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - relayed to the test thread
            out["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "call blocked instead of returning immediately"
    if "error" in out:
        raise out["error"]  # type: ignore[misc]
    return out.get("value")


def test_second_run_in_same_chat_is_rejected_immediately():
    conv, llm, gate = _gated_conv()
    first, result = _start_slow_run(conv, gate)
    try:
        history_before = [(m.role, m.content) for m in conv._chats[7]]
        with pytest.raises(ChatBusyError) as excinfo:
            _call_with_timeout(lambda: conv.handle(7, "question B"))
        assert excinfo.value.chat_id == 7
        # No LLM call for B and the history is untouched by it.
        assert len(llm.chat_calls) == 1
        assert [(m.role, m.content) for m in conv._chats[7]] == history_before
    finally:
        gate.release.set()
        first.join(5)
    assert not first.is_alive()
    assert result == {"a": "answer to slow question A"}
    assert [(m.role, m.content) for m in conv._chats[7]] == [
        ("user", "slow question A"),
        ("assistant", ""),
        ("tool", "gate result"),
        ("assistant", "answer to slow question A"),
    ]
    # Once the run finished, the chat accepts messages again.
    assert conv.handle(7, "question B") == "answer to question B"


def test_new_during_run_is_rejected_and_does_not_clear():
    conv, _, gate = _gated_conv()
    first, _ = _start_slow_run(conv, gate)
    try:
        with pytest.raises(ChatBusyError):
            _call_with_timeout(lambda: conv.handle(7, "/new"))
        # Still mid-run: user + assistant tool_call, nothing cleared.
        assert len(conv._chats[7]) == 2
    finally:
        gate.release.set()
        first.join(5)
    assert not first.is_alive()
    assert len(conv._chats[7]) == 4
    # /new works normally once the run finished.
    assert (
        conv.handle(7, "/new")
        == "Started a new conversation. Previous context cleared."
    )
    assert 7 not in conv._chats


def test_other_chats_are_not_blocked_by_a_running_chat():
    conv, _, gate = _gated_conv()
    first, _ = _start_slow_run(conv, gate)
    try:
        reply = _call_with_timeout(lambda: conv.handle(8, "question C"))
        assert reply == "answer to question C"
        assert conv._in_flight == {7}
    finally:
        gate.release.set()
        first.join(5)
    assert not first.is_alive()


def test_busy_mark_cleared_when_run_raises(monkeypatch):
    llm = ScriptedLLM([ChatResponse(content="recovered")])
    conv = ConversationManager(_make_agent(llm))
    real_run = conv._agent.run

    def boom(text: str, messages: list[Message]) -> str:
        raise RuntimeError("agent failed")

    monkeypatch.setattr(conv._agent, "run", boom)
    with pytest.raises(RuntimeError):
        conv.handle(7, "hello")
    assert conv._in_flight == set()
    monkeypatch.setattr(conv._agent, "run", real_run)
    assert conv.handle(7, "again") == "recovered"


def test_in_flight_set_is_empty_after_runs():
    conv = ConversationManager(
        _make_agent(ScriptedLLM([ChatResponse(content="x")] * 3))
    )
    for chat_id in (1, 2, 3):
        conv.handle(chat_id, "hi")
    conv.handle(1, "/new")
    assert conv._in_flight == set()


# --- reserve(): a chat held across work done before the agent turn --------------


def test_reserve_busy_chat_raises_immediately_without_waiting():
    conv, _, gate = _gated_conv()
    first, _ = _start_slow_run(conv, gate)
    try:

        def enter() -> None:
            with conv.reserve(7):
                raise AssertionError("reserved a busy chat")

        with pytest.raises(ChatBusyError):
            _call_with_timeout(enter)
    finally:
        gate.release.set()
        first.join(5)
    assert not first.is_alive()
    assert conv._in_flight == set()


def test_reservation_blocks_same_chat_not_other_chats():
    conv = ConversationManager(
        _make_agent(ScriptedLLM([ChatResponse(content="other chat")]))
    )
    with conv.reserve(7):
        assert conv._in_flight == {7}
        with pytest.raises(ChatBusyError):
            _call_with_timeout(lambda: conv.handle(7, "question"))
        with pytest.raises(ChatBusyError):
            _call_with_timeout(lambda: conv.handle(7, "/new"))
        assert _call_with_timeout(lambda: conv.handle(8, "hi")) == "other chat"
    assert conv._in_flight == set()
    assert 7 not in conv._chats


def test_reservation_handle_runs_turn_and_keeps_history():
    llm = ScriptedLLM([ChatResponse(content="first"), ChatResponse(content="second")])
    conv = ConversationManager(_make_agent(llm))
    with conv.reserve(7) as reservation:
        assert conv.handle(7, "notice", reservation=reservation) == "first"
        assert conv._in_flight == {7}  # still held after the turn
    assert conv._in_flight == set()
    assert conv.handle(7, "follow-up") == "second"
    assert [m.content for m in llm.chat_calls[1][0]] == ["notice", "first", "follow-up"]


def test_reservation_released_on_exception_and_unusable_after_exit():
    conv = ConversationManager(_make_agent(ScriptedLLM([ChatResponse(content="x")])))
    with (
        pytest.raises(RuntimeError, match="ingest failed"),
        conv.reserve(7) as reservation,
    ):
        raise RuntimeError("ingest failed")
    assert conv._in_flight == set()
    with pytest.raises(RuntimeError, match="no longer held"):
        conv.handle(7, "late", reservation=reservation)
    assert conv.handle(7, "now") == "x"


def test_reservation_handles_new_command():
    conv = ConversationManager(_make_agent(ScriptedLLM([ChatResponse(content="a")])))
    conv.handle(7, "hi")
    with conv.reserve(7) as reservation:
        assert (
            conv.handle(7, "/new", reservation=reservation)
            == "Started a new conversation. Previous context cleared."
        )
    assert 7 not in conv._chats


# --- failed runs roll the history back to its pre-turn state -------------------


class _StepLLM(LLM):
    """Plays back responses; an ``Exception`` item is raised instead."""

    def __init__(self, steps: list[ChatResponse | Exception]) -> None:
        self._steps = list(steps)
        self.chat_calls: list[list[Message]] = []

    def generate(self, message: str) -> LLMResponse:
        raise NotImplementedError

    def chat(
        self, messages: list[Message], tools: list[ToolSpec], system: str = ""
    ) -> ChatResponse:
        self.chat_calls.append(list(messages))
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_consecutive_llm_errors_leave_history_unchanged():
    from llm.base import LLMError

    n = 5
    llm = _StepLLM(
        [ChatResponse(content="first")]
        + [LLMError("down")] * n
        + [ChatResponse(content="back")]
    )
    conv = ConversationManager(Agent(llm, ToolRegistry()), max_messages=4)
    assert conv.handle(7, "hello") == "first"
    before = list(conv._chats[7])
    for i in range(n):
        with pytest.raises(LLMError):
            conv.handle(7, f"retry {i}")
        assert conv._chats[7] == before
    assert conv._in_flight == set()

    assert conv.handle(7, "again") == "back"
    sent = llm.chat_calls[-1]
    assert [(m.role, m.content) for m in sent] == [
        ("user", "hello"),
        ("assistant", "first"),
        ("user", "again"),
    ]
    assert [m.role for m in conv._chats[7]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_failed_run_rolls_back_partial_tool_messages():
    from llm.base import LLMError

    reg = ToolRegistry()
    reg.register(StubTool())
    llm = _StepLLM(
        [
            ChatResponse(
                content="", tool_calls=[ToolCall(id="c1", name="stub", arguments={})]
            ),
            LLMError("died mid-run"),
            ChatResponse(content="ok"),
        ]
    )
    conv = ConversationManager(Agent(llm, reg))
    with pytest.raises(LLMError):
        conv.handle(7, "use the tool")
    # The failed run had appended user + assistant(tool_call) + tool result.
    assert len(llm.chat_calls[1]) == 3
    assert conv._chats[7] == []

    assert conv.handle(7, "plain") == "ok"
    assert [(m.role, m.content) for m in conv._chats[7]] == [
        ("user", "plain"),
        ("assistant", "ok"),
    ]


def test_unexpected_error_also_rolls_back():
    llm = _StepLLM([ChatResponse(content="a"), RuntimeError("bug")])
    conv = ConversationManager(Agent(llm, ToolRegistry()))
    conv.handle(7, "hi")
    before = list(conv._chats[7])
    with pytest.raises(RuntimeError, match="bug"):
        conv.handle(7, "boom")
    assert conv._chats[7] == before


# --- handle(reservation=...) is the single turn entry point --------------------


def test_handle_with_reservation_runs_inside_held_mark():
    conv = ConversationManager(_make_agent(ScriptedLLM([ChatResponse(content="r")])))
    with conv.reserve(7) as reservation:
        assert conv.handle(7, "notice", reservation=reservation) == "r"
        assert conv._in_flight == {7}
    assert conv._in_flight == set()


def test_handle_rejects_mismatched_or_stale_reservation():
    conv = ConversationManager(_make_agent(ScriptedLLM([ChatResponse(content="r")])))
    other = ConversationManager(_make_agent(ScriptedLLM([])))
    with conv.reserve(7) as reservation:
        with pytest.raises(ValueError):
            conv.handle(8, "x", reservation=reservation)
        with pytest.raises(ValueError):
            other.handle(7, "x", reservation=reservation)
    with pytest.raises(RuntimeError, match="no longer held"):
        conv.handle(7, "late", reservation=reservation)
    assert 7 not in conv._chats or conv._chats[7] == []

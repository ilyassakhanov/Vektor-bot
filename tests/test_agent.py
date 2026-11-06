"""Tests for the Agent — bounded agentic loop with tools and skills.

The agent receives a user message, calls the LLM, executes any tool calls,
feeds results back, and repeats until the LLM returns a final answer or
the max iteration limit is reached.

All tests use ScriptedLLM / FakeLLM — no Ollama dependency.
"""

from __future__ import annotations

import itertools

import pytest
from prometheus_client import REGISTRY

from agent.agent import (
    BUDGET_EXCEEDED_REPLY,
    MAX_ITERATIONS_REPLY,
    SKIPPED_BUDGET_RESULT,
    SKIPPED_TOO_MANY_RESULT,
    Agent,
)
from llm.base import ChatResponse, LLMError, Message, ToolCall
from tests.fakes import ScriptedLLM
from tools.base import Tool, ToolError
from tools.registry import ToolRegistry


class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo the message argument."

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }

    def execute(self, **kwargs: object) -> str:
        return str(kwargs.get("message", ""))


def _make_registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


# --- Normal Agent request (no tools) ---------------------------------------


def test_agent_returns_final_answer():
    llm = ScriptedLLM([ChatResponse(content="Here is your answer.")])
    agent = Agent(llm, _make_registry())
    result = agent.run("What is 2+2?")
    assert result == "Here is your answer."
    assert len(llm.chat_calls) == 1


def test_agent_passes_user_message_in_context():
    llm = ScriptedLLM([ChatResponse(content="answer")])
    agent = Agent(llm, _make_registry())
    agent.run("hello world")
    msgs = llm.chat_calls[0][0]
    assert msgs[-1].role == "user"
    assert msgs[-1].content == "hello world"


def test_agent_passes_tools_to_llm():
    llm = ScriptedLLM([ChatResponse(content="answer")])
    reg = _make_registry(EchoTool())
    agent = Agent(llm, reg)
    agent.run("hi")
    tools_passed = llm.chat_calls[0][1]
    assert len(tools_passed) == 1
    assert tools_passed[0].name == "echo"


def test_agent_passes_system_prompt_with_skills():
    llm = ScriptedLLM([ChatResponse(content="answer")])
    agent = Agent(llm, _make_registry(), system_prompt="You are a CVE analyst.")
    agent.run("hi")
    system = llm.chat_calls[0][2]
    assert "CVE analyst" in system


# --- Tool call → execution → result → next LLM call ------------------------


def test_agent_executes_tool_call_and_feeds_result():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "hello"})
                ],
            ),
            ChatResponse(content="The echo said: hello"),
        ]
    )
    agent = Agent(llm, _make_registry(EchoTool()))
    result = agent.run("echo something")
    assert result == "The echo said: hello"
    # Two LLM calls: first returns tool call, second returns final answer
    assert len(llm.chat_calls) == 2
    # Second call should include the tool result
    second_msgs = llm.chat_calls[1][0]
    tool_msgs = [m for m in second_msgs if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "hello" in tool_msgs[0].content
    assert tool_msgs[0].tool_call_id == "tc1"


def test_agent_assistant_message_with_tool_calls_in_context():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "hi"})
                ],
            ),
            ChatResponse(content="done"),
        ]
    )
    agent = Agent(llm, _make_registry(EchoTool()))
    agent.run("go")
    second_msgs = llm.chat_calls[1][0]
    assistant_msgs = [m for m in second_msgs if m.role == "assistant"]
    # The last assistant message before the tool result should have the tool calls
    assert any(m.tool_calls for m in assistant_msgs)


# --- Multiple iterations ----------------------------------------------------


def test_agent_multiple_tool_calls_sequential():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "first"})
                ],
            ),
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc2", name="echo", arguments={"message": "second"})
                ],
            ),
            ChatResponse(content="Both done: first and second"),
        ]
    )
    agent = Agent(llm, _make_registry(EchoTool()))
    result = agent.run("echo twice")
    assert result == "Both done: first and second"
    assert len(llm.chat_calls) == 3


def test_agent_multiple_tool_calls_in_single_response():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "a"}),
                    ToolCall(id="tc2", name="echo", arguments={"message": "b"}),
                ],
            ),
            ChatResponse(content="Got a and b"),
        ]
    )
    agent = Agent(llm, _make_registry(EchoTool()))
    result = agent.run("echo two things")
    assert result == "Got a and b"
    # Second LLM call should have both tool results
    second_msgs = llm.chat_calls[1][0]
    tool_msgs = [m for m in second_msgs if m.role == "tool"]
    assert len(tool_msgs) == 2


# --- Maximum iteration protection ------------------------------------------


def test_agent_max_iterations_default_is_8():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id=f"tc{i}", name="echo", arguments={"message": "x"})
                ],
            )
            for i in range(100)
        ]
    )
    agent = Agent(llm, _make_registry(EchoTool()))
    result = agent.run("loop")
    assert (
        "maximum" in result.lower()
        or "max" in result.lower()
        or "iteration" in result.lower()
    )


def test_agent_max_iterations_configurable():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id=f"tc{i}", name="echo", arguments={"message": "x"})
                ],
            )
            for i in range(100)
        ]
    )
    agent = Agent(llm, _make_registry(EchoTool()), max_iterations=3)
    result = agent.run("loop")
    assert (
        "maximum" in result.lower()
        or "max" in result.lower()
        or "iteration" in result.lower()
    )
    assert len(llm.chat_calls) == 3


def test_max_iterations_reply_template_constant():
    reply = MAX_ITERATIONS_REPLY.format(max_iterations=3)
    assert "3" in reply
    assert "maximum" in reply.lower()


def test_agent_fallback_reply_matches_constant():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id=f"tc{i}", name="echo", arguments={"message": "x"})
                ],
            )
            for i in range(100)
        ]
    )
    agent = Agent(llm, _make_registry(EchoTool()), max_iterations=3)
    result = agent.run("loop")
    assert result == MAX_ITERATIONS_REPLY.format(max_iterations=3)


def test_agent_stops_when_max_reached_even_without_tool_calls():
    """If the LLM keeps returning empty content without tool calls, the agent
    should not loop forever."""
    llm = ScriptedLLM([ChatResponse(content="") for _ in range(100)])
    agent = Agent(llm, _make_registry(), max_iterations=3)
    result = agent.run("loop")
    # Empty content without tool calls should be treated as a final answer
    assert result == ""


def test_agent_never_infinite_loop():
    """Safety: even with a misbehaving LLM, the agent must terminate."""
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id=f"tc{i}", name="echo", arguments={"message": "x"})
                ],
            )
            for i in range(100000)
        ]
    )
    agent = Agent(llm, _make_registry(EchoTool()), max_iterations=5)
    result = agent.run("loop forever")
    assert isinstance(result, str)


# --- Tool failure -----------------------------------------------------------


class CrashingTool(Tool):
    @property
    def name(self) -> str:
        return "crash"

    @property
    def description(self) -> str:
        return "Crashes."

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs: object) -> str:
        raise RuntimeError("Unexpected crash!")


class FailingTool(Tool):
    @property
    def name(self) -> str:
        return "fail"

    @property
    def description(self) -> str:
        return "Fails."

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs: object) -> str:
        raise ToolError("Tool failed!")


def test_agent_tool_failure_returned_to_llm():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="crash", arguments={})],
            ),
            ChatResponse(content="The tool crashed but I handled it."),
        ]
    )
    agent = Agent(llm, _make_registry(CrashingTool()))
    result = agent.run("use the crash tool")
    assert result == "The tool crashed but I handled it."
    second_msgs = llm.chat_calls[1][0]
    tool_msgs = [m for m in second_msgs if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert (
        "crash" in tool_msgs[0].content.lower()
        or "error" in tool_msgs[0].content.lower()
    )


def test_agent_tool_error_returned_to_llm():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fail", arguments={})],
            ),
            ChatResponse(content="The tool failed."),
        ]
    )
    agent = Agent(llm, _make_registry(FailingTool()))
    result = agent.run("use the fail tool")
    assert result == "The tool failed."
    second_msgs = llm.chat_calls[1][0]
    tool_msgs = [m for m in second_msgs if m.role == "tool"]
    assert "failed" in tool_msgs[0].content.lower()


def test_agent_unknown_tool_returns_error_to_llm():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="nonexistent", arguments={})],
            ),
            ChatResponse(content="That tool doesn't exist."),
        ]
    )
    agent = Agent(llm, _make_registry())
    result = agent.run("use nonexistent tool")
    assert result == "That tool doesn't exist."
    second_msgs = llm.chat_calls[1][0]
    tool_msgs = [m for m in second_msgs if m.role == "tool"]
    assert "not found" in tool_msgs[0].content.lower()


# --- LLM error --------------------------------------------------------------


def test_agent_llm_error_raises():
    from tests.fakes import FakeLLM

    llm = FakeLLM(error=LLMError("LLM crashed"))
    agent = Agent(llm, _make_registry())
    with pytest.raises(LLMError, match="LLM crashed"):
        agent.run("hi")


# --- Agent has no Ollama dependency -----------------------------------------


def test_agent_works_with_fake_llm():
    from tests.fakes import FakeLLM

    llm = FakeLLM(reply="fake response")
    agent = Agent(llm, _make_registry())
    result = agent.run("hi")
    assert result == "fake response"


# --- Agent iteration metrics -------------------------------------------------


def _sample(name: str) -> float:
    return REGISTRY.get_sample_value(name) or 0.0


def test_agent_records_iteration_count():
    llm = ScriptedLLM([ChatResponse(content="answer")])
    agent = Agent(llm, _make_registry())
    count_before = _sample("vektor_agent_iterations_count")
    sum_before = _sample("vektor_agent_iterations_sum")
    agent.run("hi")
    assert _sample("vektor_agent_iterations_count") - count_before == 1.0
    assert _sample("vektor_agent_iterations_sum") - sum_before == 1.0


def test_agent_records_iteration_count_multi():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "a"})
                ],
            ),
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc2", name="echo", arguments={"message": "b"})
                ],
            ),
            ChatResponse(content="done"),
        ]
    )
    agent = Agent(llm, _make_registry(EchoTool()))
    count_before = _sample("vektor_agent_iterations_count")
    sum_before = _sample("vektor_agent_iterations_sum")
    result = agent.run("go")
    assert result == "done"
    assert _sample("vektor_agent_iterations_sum") - sum_before == 3.0
    assert _sample("vektor_agent_iterations_count") - count_before == 1.0


def test_agent_records_max_iterations_reached():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id=f"tc{i}", name="echo", arguments={"message": "x"})
                ],
            )
            for i in range(100)
        ]
    )
    agent = Agent(llm, _make_registry(EchoTool()), max_iterations=3)
    max_before = _sample("vektor_agent_max_iterations_reached_total")
    sum_before = _sample("vektor_agent_iterations_sum")
    agent.run("loop")
    assert _sample("vektor_agent_max_iterations_reached_total") - max_before == 1.0
    assert _sample("vektor_agent_iterations_sum") - sum_before == 3.0


def test_agent_max_iterations_not_reached_on_normal():
    llm = ScriptedLLM([ChatResponse(content="answer")])
    agent = Agent(llm, _make_registry())
    max_before = _sample("vektor_agent_max_iterations_reached_total")
    agent.run("hi")
    assert _sample("vektor_agent_max_iterations_reached_total") - max_before == 0.0


# --- Prompt-cache prefix stability -------------------------------------------


def test_agent_system_prompt_identical_across_turns():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "a"})
                ],
            ),
            ChatResponse(content="first answer"),
            ChatResponse(content="second answer"),
        ]
    )
    agent = Agent(
        llm,
        _make_registry(EchoTool()),
        system_prompt="You are a CVE analyst. Use get_latest_cve. Never fabricate.",
    )
    messages: list[Message] = []
    agent.run("first question", messages)
    agent.run("second question", messages)
    assert len(llm.chat_calls) == 3
    systems = [call[2] for call in llm.chat_calls]
    assert systems[0] != ""
    assert systems == [systems[0]] * len(systems)


def test_agent_message_prefix_stable_across_iterations():
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "a"})
                ],
            ),
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc2", name="echo", arguments={"message": "b"})
                ],
            ),
            ChatResponse(content="done"),
        ]
    )
    agent = Agent(llm, _make_registry(EchoTool()), system_prompt="system")
    agent.run("go")
    calls = [call[0] for call in llm.chat_calls]
    assert len(calls) == 3
    for prev, cur in itertools.pairwise(calls):
        assert cur[: len(prev)] == prev


# --- Wall-clock budget ---------------------------------------------------------


def _looping_llm(n: int = 100) -> ScriptedLLM:
    return ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id=f"tc{i}", name="echo", arguments={"message": "x"})
                ],
            )
            for i in range(n)
        ]
    )


def _stepping_clock(step: float, start: float = 0.0):
    counter = itertools.count()
    return lambda: start + step * next(counter)


def test_budget_stops_before_the_call_that_would_exceed_it():
    llm = _looping_llm()
    agent = Agent(
        llm,
        _make_registry(EchoTool()),
        max_iterations=8,
        max_run_seconds=120,
        clock=_stepping_clock(50.0),
    )
    messages: list[Message] = []
    result = agent.run("loop", messages)
    # start=0; tool check 50, iteration check 100 pass; the tool check at
    # 150 trips (its call is skipped) before a third LLM call.
    assert len(llm.chat_calls) == 2
    assert messages[-2].content == SKIPPED_BUDGET_RESULT
    assert result == BUDGET_EXCEEDED_REPLY.format(max_run_seconds=120)
    assert messages[-1] == Message(role="assistant", content=result)


def test_budget_none_keeps_max_iterations_behavior():
    llm = _looping_llm()
    agent = Agent(
        llm,
        _make_registry(EchoTool()),
        max_iterations=3,
        max_run_seconds=None,
        clock=_stepping_clock(1000.0),
    )
    assert agent.run("loop") == MAX_ITERATIONS_REPLY.format(max_iterations=3)
    assert len(llm.chat_calls) == 3


@pytest.mark.parametrize("budget", [0, -5])
def test_non_positive_budget_disables_the_check(budget: float):
    llm = _looping_llm()
    agent = Agent(
        llm,
        _make_registry(EchoTool()),
        max_iterations=2,
        max_run_seconds=budget,
        clock=_stepping_clock(1000.0),
    )
    agent.run("loop")
    assert len(llm.chat_calls) == 2


def test_first_iteration_always_runs_even_with_a_late_clock():
    llm = ScriptedLLM([ChatResponse(content="answer")])
    agent = Agent(
        llm,
        _make_registry(),
        max_run_seconds=1,
        clock=_stepping_clock(10_000.0),
    )
    assert agent.run("hi") == "answer"
    assert len(llm.chat_calls) == 1


def test_budget_metric_incremented_once():
    before = _sample("vektor_agent_budget_exceeded_total")
    agent = Agent(
        _looping_llm(),
        _make_registry(EchoTool()),
        max_run_seconds=120,
        clock=_stepping_clock(50.0),
    )
    agent.run("loop")
    assert _sample("vektor_agent_budget_exceeded_total") - before == 1.0


def test_budget_reply_formats_whole_seconds():
    assert "120s" in BUDGET_EXCEEDED_REPLY.format(max_run_seconds=120)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 120.0),
        ("30", 30.0),
        ("2.5", 2.5),
        ("0", 0.0),
        ("abc", 120.0),
        ("nan", 120.0),
        ("inf", 120.0),
    ],
)
def test_build_agent_reads_max_run_seconds(monkeypatch, value, expected):
    import bot

    if value is None:
        monkeypatch.delenv("AGENT_MAX_RUN_SECONDS", raising=False)
    else:
        monkeypatch.setenv("AGENT_MAX_RUN_SECONDS", value)
    agent = bot.build_agent(ScriptedLLM([]), _make_registry())
    assert agent._max_run_seconds == expected


# --- Per-tool-call budget and tool-call cap ------------------------------------


class CountingTool(EchoTool):
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, **kwargs: object) -> str:
        self.calls += 1
        return super().execute(**kwargs)


def _many_calls(n: int, prefix: str = "tc") -> ChatResponse:
    return ChatResponse(
        content="",
        tool_calls=[
            ToolCall(id=f"{prefix}{i}", name="echo", arguments={"message": str(i)})
            for i in range(n)
        ],
    )


def test_budget_checked_before_each_tool_call_in_one_reply():
    llm = ScriptedLLM([_many_calls(5), ChatResponse(content="never")])
    tool = CountingTool()
    agent = Agent(
        llm,
        _make_registry(tool),
        max_run_seconds=120,
        max_tool_calls_per_turn=None,
        clock=_stepping_clock(50.0),
    )
    messages: list[Message] = []
    result = agent.run("loop", messages)
    # start=0; tool checks at 50, 100 pass; the check at 150 trips.
    assert tool.calls == 2
    assert len(llm.chat_calls) == 1
    assert result == BUDGET_EXCEEDED_REPLY.format(max_run_seconds=120)
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == [f"tc{i}" for i in range(5)]
    assert [m.content for m in tool_msgs[2:]] == [SKIPPED_BUDGET_RESULT] * 3
    assert messages[-1] == Message(role="assistant", content=result)


def test_budget_trip_inside_a_turn_increments_metric_once():
    before = _sample("vektor_agent_budget_exceeded_total")
    agent = Agent(
        ScriptedLLM([_many_calls(5)]),
        _make_registry(CountingTool()),
        max_run_seconds=120,
        max_tool_calls_per_turn=None,
        clock=_stepping_clock(50.0),
    )
    agent.run("loop")
    assert _sample("vektor_agent_budget_exceeded_total") - before == 1.0


def test_tool_calls_per_turn_are_capped():
    llm = ScriptedLLM([_many_calls(6), ChatResponse(content="done")])
    tool = CountingTool()
    agent = Agent(llm, _make_registry(tool), max_tool_calls_per_turn=2)
    assert agent.run("go") == "done"
    assert tool.calls == 2
    second = llm.chat_calls[1][0]
    tool_msgs = [m for m in second if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == [f"tc{i}" for i in range(6)]
    skipped = SKIPPED_TOO_MANY_RESULT.format(max_calls=2)
    assert [m.content for m in tool_msgs[2:]] == [skipped] * 4


def test_default_tool_call_cap_is_four():
    llm = ScriptedLLM([_many_calls(10), ChatResponse(content="done")])
    tool = CountingTool()
    Agent(llm, _make_registry(tool)).run("go")
    assert tool.calls == 4


@pytest.mark.parametrize(
    ("value", "expected"), [(None, 4), ("2", 2), ("0", 4), ("-1", 4), ("x", 4)]
)
def test_build_agent_reads_max_tool_calls_per_turn(monkeypatch, value, expected):
    import bot

    if value is None:
        monkeypatch.delenv("AGENT_MAX_TOOL_CALLS_PER_TURN", raising=False)
    else:
        monkeypatch.setenv("AGENT_MAX_TOOL_CALLS_PER_TURN", value)
    agent = bot.build_agent(ScriptedLLM([]), _make_registry())
    assert agent._max_tool_calls_per_turn == expected

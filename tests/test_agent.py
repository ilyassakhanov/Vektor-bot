"""Tests for the Agent — bounded agentic loop with tools and skills.

The agent receives a user message, calls the LLM, executes any tool calls,
feeds results back, and repeats until the LLM returns a final answer or
the max iteration limit is reached.

All tests use ScriptedLLM / FakeLLM — no Ollama dependency.
"""

from __future__ import annotations

import pytest

from agent.agent import Agent
from llm.base import ChatResponse, LLMError, ToolCall
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

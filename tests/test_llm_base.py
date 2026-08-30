"""Tests for the extended LLM abstraction types."""

from __future__ import annotations

from llm.base import (
    LLM,
    ChatResponse,
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
)


def test_llm_response_has_text():
    r = LLMResponse(text="hello")
    assert r.text == "hello"


def test_tool_spec_fields():
    spec = ToolSpec(
        name="exec",
        description="Run a shell command",
        parameters={"type": "object", "properties": {}},
    )
    assert spec.name == "exec"
    assert spec.description == "Run a shell command"
    assert spec.parameters["type"] == "object"


def test_tool_call_fields():
    tc = ToolCall(id="tc1", name="exec", arguments={"command": "echo hi"})
    assert tc.id == "tc1"
    assert tc.name == "exec"
    assert tc.arguments["command"] == "echo hi"


def test_tool_result_defaults():
    tr = ToolResult(tool_call_id="tc1", content="output")
    assert tr.tool_call_id == "tc1"
    assert tr.content == "output"
    assert tr.is_error is False


def test_message_defaults():
    msg = Message(role="user", content="hello")
    assert msg.role == "user"
    assert msg.content == "hello"
    assert msg.tool_calls == []
    assert msg.tool_call_id is None


def test_message_with_tool_calls():
    tc = ToolCall(id="tc1", name="exec", arguments={"command": "ls"})
    msg = Message(role="assistant", content="", tool_calls=[tc])
    assert msg.role == "assistant"
    assert msg.tool_calls == [tc]


def test_message_tool_result_role():
    msg = Message(role="tool", content="output", tool_call_id="tc1")
    assert msg.role == "tool"
    assert msg.content == "output"
    assert msg.tool_call_id == "tc1"


def test_chat_response_with_final_answer():
    cr = ChatResponse(content="Here is the CVE summary")
    assert cr.content == "Here is the CVE summary"
    assert cr.tool_calls == []


def test_chat_response_with_tool_calls():
    tc = ToolCall(id="tc1", name="exec", arguments={"command": "curl ..."})
    cr = ChatResponse(content="", tool_calls=[tc])
    assert cr.tool_calls == [tc]


def test_llm_is_abstract():
    import pytest

    with pytest.raises(TypeError):
        LLM()  # type: ignore[abstract]


def test_generate_still_in_interface():
    assert hasattr(LLM, "generate")
    assert hasattr(LLM, "chat")

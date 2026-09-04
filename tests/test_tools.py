"""Tests for the Tool abstraction and ToolRegistry."""

from __future__ import annotations

import pytest

from tools.base import Tool, ToolError
from tools.registry import ToolRegistry


class EchoTool(Tool):
    """Test-only tool that echoes its command argument."""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo back the command argument."

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }

    def execute(self, **kwargs: object) -> str:
        return str(kwargs.get("command", ""))


class FailingTool(Tool):
    """Test-only tool that always raises."""

    @property
    def name(self) -> str:
        return "fail"

    @property
    def description(self) -> str:
        return "Always fails."

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs: object) -> str:
        raise ToolError("boom")


# --- Tool base class --------------------------------------------------------


def test_tool_is_abstract():
    with pytest.raises(TypeError):
        Tool()  # type: ignore[abstract]


def test_echo_tool_execute():
    t = EchoTool()
    assert t.execute(command="hello") == "hello"


# --- ToolRegistry -----------------------------------------------------------


def test_registry_register_and_get():
    reg = ToolRegistry()
    tool = EchoTool()
    reg.register(tool)
    assert reg.get("echo") is tool


def test_registry_get_unknown_returns_none():
    reg = ToolRegistry()
    assert reg.get("nonexistent") is None


def test_registry_list_tool_specs():
    reg = ToolRegistry()
    reg.register(EchoTool())
    specs = reg.specs()
    assert len(specs) == 1
    assert specs[0].name == "echo"
    assert specs[0].description == "Echo back the command argument."
    assert specs[0].parameters["type"] == "object"


def test_registry_multiple_tools():
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(FailingTool())
    specs = reg.specs()
    assert len(specs) == 2
    names = {s.name for s in specs}
    assert names == {"echo", "fail"}


def test_registry_execute_calls_tool():
    reg = ToolRegistry()
    reg.register(EchoTool())
    result = reg.execute("echo", command="hello")
    assert result == "hello"


def test_registry_execute_unknown_tool_returns_error():
    reg = ToolRegistry()
    result = reg.execute("nonexistent", command="x")
    assert "error" in result.lower() or "not found" in result.lower()


def test_registry_execute_tool_failure_returns_error_not_raise():
    reg = ToolRegistry()
    reg.register(FailingTool())
    result = reg.execute("fail")
    assert "boom" in result
    assert "error" in result.lower() or "fail" in result.lower()


def test_registry_no_tools_empty_specs():
    reg = ToolRegistry()
    assert reg.specs() == []


def test_adding_tool_does_not_require_loop_change():
    """The registry pattern: loop calls reg.execute(name, **args) — new tools
    only need register(), not loop changes."""
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(FailingTool())
    # Simulate what the agent loop does
    for spec in reg.specs():
        result = reg.execute(spec.name, command="test")
        assert isinstance(result, str)

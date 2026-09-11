"""Tests for InstrumentedToolRegistry — tool call metrics recording.

Metrics live in the global REGISTRY and accumulate across tests, so each
test uses unique tool names and asserts before/after deltas.
"""

from __future__ import annotations

from prometheus_client import REGISTRY

from tools.base import Tool, ToolError
from tools.instrumented_registry import InstrumentedToolRegistry
from tools.registry import ToolRegistry


def _calls(tool_name: str, status: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "vektor_tool_calls_total", {"tool_name": tool_name, "status": status}
        )
        or 0.0
    )


def _duration_count(tool_name: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "vektor_tool_duration_seconds_count", {"tool_name": tool_name}
        )
        or 0.0
    )


class NamedEchoTool(Tool):
    """Echo tool with a configurable name (unique per test)."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

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


class NamedFailingTool(NamedEchoTool):
    """Tool that always raises ToolError."""

    def execute(self, **kwargs: object) -> str:
        raise ToolError("boom")


def test_records_success():
    reg = InstrumentedToolRegistry()
    reg.register(NamedEchoTool("echo_t5a"))
    calls_before = _calls("echo_t5a", "success")
    duration_before = _duration_count("echo_t5a")
    result = reg.execute("echo_t5a", message="hi")
    assert result == "hi"
    assert _calls("echo_t5a", "success") - calls_before == 1.0
    assert _duration_count("echo_t5a") - duration_before == 1.0


def test_records_error_status():
    reg = InstrumentedToolRegistry()
    reg.register(NamedFailingTool("fail_t5b"))
    before = _calls("fail_t5b", "error")
    result = reg.execute("fail_t5b")
    assert result.startswith("Error:")
    assert _calls("fail_t5b", "error") - before == 1.0


def test_records_unknown_tool():
    reg = InstrumentedToolRegistry()
    before = _calls("nonexistent_t5c", "error")
    result = reg.execute("nonexistent_t5c")
    assert "not found" in result
    assert _calls("nonexistent_t5c", "error") - before == 1.0


def test_specs_delegated():
    tool = NamedEchoTool("echo_t5d")
    plain = ToolRegistry()
    plain.register(tool)
    instrumented = InstrumentedToolRegistry()
    instrumented.register(tool)
    assert instrumented.specs() == plain.specs()


def test_duration_recorded_on_error_too():
    reg = InstrumentedToolRegistry()
    reg.register(NamedFailingTool("fail_t5e"))
    before = _duration_count("fail_t5e")
    reg.execute("fail_t5e")
    assert _duration_count("fail_t5e") - before == 1.0


def test_register_delegates():
    reg = InstrumentedToolRegistry()
    tool = NamedEchoTool("echo_t5f")
    reg.register(tool)
    assert reg.get("echo_t5f") is tool
    assert reg.execute("echo_t5f", message="ok") == "ok"

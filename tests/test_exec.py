"""Tests for ExecTool — generic shell execution with timeout."""

from __future__ import annotations

import time

import pytest

from tools.base import ToolError
from tools.exec import ExecTool

# --- Basic execution --------------------------------------------------------


def test_exec_returns_stdout():
    tool = ExecTool(timeout=10.0)
    result = tool.execute(command="echo hello")
    assert "hello" in result
    assert "exit_code: 0" in result


def test_exec_returns_stderr_and_exit_code():
    tool = ExecTool(timeout=10.0)
    result = tool.execute(command="echo error >&2 && exit 3")
    assert "exit_code: 3" in result
    assert "error" in result


def test_exec_result_contains_all_three_fields():
    tool = ExecTool(timeout=10.0)
    result = tool.execute(command="echo out; echo err >&2")
    assert "stdout:" in result
    assert "stderr:" in result
    assert "exit_code:" in result


def test_exec_with_multiline_output():
    tool = ExecTool(timeout=10.0)
    result = tool.execute(command="echo line1; echo line2; echo line3")
    assert "line1" in result
    assert "line2" in result
    assert "line3" in result


# --- Timeout ----------------------------------------------------------------


def test_exec_timeout_returns_error_not_crash():
    tool = ExecTool(timeout=0.5)
    result = tool.execute(command="sleep 10")
    assert "timeout" in result.lower() or "timed out" in result.lower()
    assert "exit_code" in result


def test_exec_timeout_does_not_hang():
    tool = ExecTool(timeout=0.3)
    start = time.monotonic()
    tool.execute(command="sleep 10")
    elapsed = time.monotonic() - start
    assert elapsed < 2.0


# --- Missing arguments ------------------------------------------------------


def test_exec_missing_command_argument():
    tool = ExecTool(timeout=10.0)
    with pytest.raises(ToolError):
        tool.execute()


def test_exec_empty_command():
    tool = ExecTool(timeout=10.0)
    result = tool.execute(command="")
    assert "exit_code" in result


# --- Tool metadata ----------------------------------------------------------


def test_exec_tool_name():
    tool = ExecTool(timeout=10.0)
    assert tool.name == "exec"


def test_exec_tool_description():
    tool = ExecTool(timeout=10.0)
    assert "shell" in tool.description.lower() or "command" in tool.description.lower()


def test_exec_tool_parameters_has_command():
    tool = ExecTool(timeout=10.0)
    params = tool.parameters
    assert params["type"] == "object"
    assert "command" in params["properties"]
    assert "command" in params["required"]


# --- Does not crash on bad commands ----------------------------------------


def test_exec_command_not_found():
    tool = ExecTool(timeout=10.0)
    result = tool.execute(command="nonexistent_command_xyz123")
    assert "exit_code" in result
    # Should not crash — should return the error
    assert isinstance(result, str)


# --- Configurable timeout ---------------------------------------------------


def test_exec_custom_timeout():
    tool = ExecTool(timeout=1.0)
    result = tool.execute(command="echo fast")
    assert "fast" in result

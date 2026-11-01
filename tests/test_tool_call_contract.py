"""Offline provider tool-call parse contract tests (pin EXISTING behavior).

Pins ``llm/ollama.py::_parse_tool_call`` via ``httpx.MockTransport`` (the
``tests/test_ollama_chat.py`` idiom): tool-call arguments that fail JSON
parsing fall back to ``{"_raw": args}``; non-dict parsed arguments become
``{}``; wrong-arity arguments reach the real registry and the error string
is fed back to the LLM as the tool-role message of the next request.

These tests pin existing behavior — a failure means the parse contract
regressed (or was misunderstood), not a license to change ``llm/ollama.py``.
See openspec/changes/2026-10-22-agent-system-prompt/.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agent.agent import Agent
from llm.base import Message
from llm.ollama import OllamaLLM
from tools.exec import ExecTool
from tools.registry import ToolRegistry


def _make_client(body: dict[str, Any]) -> httpx.Client:
    """Single canned-response client (``tests/test_ollama_chat.py`` idiom)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


def _tool_call_body(arguments: Any, *, include_arguments: bool) -> dict[str, Any]:
    function: dict[str, Any] = {"name": "exec"}
    if include_arguments:
        function["arguments"] = arguments
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": function}],
        },
    }


# --- String arguments that fail JSON parsing → {"_raw": args} ----------------


def test_unparseable_json_string_arguments_fall_back_to_raw() -> None:
    broken = '{command: "ls -la"'
    client = _make_client(_tool_call_body(broken, include_arguments=True))
    llm = OllamaLLM(client=client)
    result = llm.chat([Message(role="user", content="run")], tools=[])
    assert result.tool_calls[0].arguments == {"_raw": broken}


# --- Non-dict arguments (valid JSON or absent) → {} --------------------------


@pytest.mark.parametrize(
    ("arguments", "include_arguments"),
    [
        pytest.param('"just a string"', True, id="json-string"),
        pytest.param("[1, 2]", True, id="json-list"),
        pytest.param("42", True, id="json-number"),
        pytest.param(None, False, id="absent"),
    ],
)
def test_non_dict_arguments_become_empty_dict(
    arguments: str | None, include_arguments: bool
) -> None:
    client = _make_client(
        _tool_call_body(arguments, include_arguments=include_arguments)
    )
    llm = OllamaLLM(client=client)
    result = llm.chat([Message(role="user", content="run")], tools=[])
    assert result.tool_calls[0].arguments == {}


# --- Wrong arity end-to-end: parse → registry error → fed back to the LLM ----


def test_wrong_arity_arguments_reach_registry_and_error_feeds_back() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            # (a) tool call with {} arguments — missing the required command.
            return httpx.Response(
                200,
                json={
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "exec",
                                    "arguments": {},
                                },
                            }
                        ],
                    },
                },
            )
        # (b) final text answer after the error is fed back.
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "handled"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    llm = OllamaLLM(client=client)
    reg = ToolRegistry()
    reg.register(ExecTool(timeout=5.0))
    agent = Agent(llm, reg)

    result = agent.run("go")

    assert result == "handled"
    assert len(captured) == 2
    tool_messages = [m for m in captured[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["content"].startswith("Error: tool 'exec' failed")

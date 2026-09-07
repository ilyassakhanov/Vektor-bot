"""Tests for OllamaLLM.chat() — multi-turn chat with tools via httpx MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from llm import LLMError
from llm.base import Message, ToolSpec
from llm.ollama import OllamaLLM


def _make_client(response: httpx.Response | Exception) -> httpx.Client:
    if isinstance(response, Exception):

        def handler(req: httpx.Request) -> httpx.Response:
            raise response
    else:

        def handler(req: httpx.Request) -> httpx.Response:
            return response

    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


# --- Success cases ----------------------------------------------------------


def test_chat_returns_final_text_answer():
    body = {
        "message": {
            "role": "assistant",
            "content": "Here is the CVE summary.",
        },
    }
    client = _make_client(httpx.Response(200, json=body))
    llm = OllamaLLM(base_url="http://ollama:11434", model="llama3.2", client=client)
    result = llm.chat([Message(role="user", content="hi")], tools=[])
    assert result.content == "Here is the CVE summary."
    assert result.tool_calls == []


def test_chat_returns_tool_calls():
    body = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "exec",
                        "arguments": {"command": "echo hi"},
                    },
                }
            ],
        },
    }
    client = _make_client(httpx.Response(200, json=body))
    llm = OllamaLLM(client=client)
    result = llm.chat([Message(role="user", content="run a command")], tools=[])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].name == "exec"
    assert result.tool_calls[0].arguments == {"command": "echo hi"}


def test_chat_sends_tools_and_system_and_messages():
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": "ok"}}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    llm = OllamaLLM(base_url="http://my-ollama:1234", model="my-model", client=client)
    tools = [
        ToolSpec(
            name="exec",
            description="Run a shell command",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        )
    ]
    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi"),
        Message(role="tool", content="result", tool_call_id="tc1"),
    ]
    llm.chat(messages, tools, system="You are a helpful assistant.")
    assert captured["url"] == "http://my-ollama:1234/api/chat"
    body = captured["body"]
    assert body["model"] == "my-model"
    assert body["stream"] is False
    assert body["system"] == "You are a helpful assistant."
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "exec",
                "description": "Run a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            },
        }
    ]
    msgs = body["messages"]
    assert len(msgs) == 3
    assert msgs[0] == {"role": "user", "content": "hello"}
    assert msgs[1] == {"role": "assistant", "content": "hi"}
    assert msgs[2] == {"role": "tool", "content": "result", "tool_call_id": "tc1"}


# --- Error cases (mirror generate() error handling) -------------------------


def test_chat_connection_failure():
    client = _make_client(httpx.ConnectError("refused"))
    llm = OllamaLLM(client=client)
    with pytest.raises(LLMError, match="Cannot connect"):
        llm.chat([Message(role="user", content="hi")], tools=[])


def test_chat_timeout_failure():
    client = _make_client(httpx.TimeoutException("slow"))
    llm = OllamaLLM(client=client)
    with pytest.raises(LLMError, match="timed out"):
        llm.chat([Message(role="user", content="hi")], tools=[])


def test_chat_http_error():
    client = _make_client(httpx.Response(500))
    llm = OllamaLLM(client=client)
    with pytest.raises(LLMError, match="LLM service error"):
        llm.chat([Message(role="user", content="hi")], tools=[])


def test_chat_malformed_response():
    client = _make_client(httpx.Response(200, content=b"not-json"))
    llm = OllamaLLM(client=client)
    with pytest.raises(LLMError, match="Malformed"):
        llm.chat([Message(role="user", content="hi")], tools=[])


def test_chat_empty_message():
    client = _make_client(httpx.Response(200, json={"message": {"content": ""}}))
    llm = OllamaLLM(client=client)
    result = llm.chat([Message(role="user", content="hi")], tools=[])
    assert result.content == ""
    assert result.tool_calls == []


def test_chat_tool_call_arguments_as_json_string():
    """Ollama sometimes returns arguments as a JSON string, not a dict."""
    body = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "exec",
                        "arguments": '{"command": "ls -la"}',
                    },
                }
            ],
        },
    }
    client = _make_client(httpx.Response(200, json=body))
    llm = OllamaLLM(client=client)
    result = llm.chat([Message(role="user", content="run")], tools=[])
    assert result.tool_calls[0].arguments == {"command": "ls -la"}


def test_chat_tool_call_without_id_gets_generated_id():
    body = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "exec",
                        "arguments": {"command": "pwd"},
                    },
                }
            ],
        },
    }
    client = _make_client(httpx.Response(200, json=body))
    llm = OllamaLLM(client=client)
    result = llm.chat([Message(role="user", content="run")], tools=[])
    assert result.tool_calls[0].id != ""
    assert len(result.tool_calls[0].id) > 0

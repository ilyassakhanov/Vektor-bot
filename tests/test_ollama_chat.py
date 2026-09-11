"""Tests for OllamaLLM.chat() — multi-turn chat with tools via httpx MockTransport."""

from __future__ import annotations

import json
import logging
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


def _capture_client(captured: dict[str, Any]) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": "ok"}}
        )

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
    assert "system" not in body
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
    assert len(msgs) == 4
    assert msgs[0] == {"role": "system", "content": "You are a helpful assistant."}
    assert msgs[1] == {"role": "user", "content": "hello"}
    assert msgs[2] == {"role": "assistant", "content": "hi"}
    assert msgs[3] == {"role": "tool", "content": "result", "tool_call_id": "tc1"}


def test_chat_system_sent_as_message():
    captured: dict[str, Any] = {}
    client = _capture_client(captured)
    llm = OllamaLLM(client=client)
    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi"),
    ]
    llm.chat(messages, tools=[], system="You are a bot.")
    body = captured["body"]
    assert "system" not in body
    msgs = body["messages"]
    assert msgs[0] == {"role": "system", "content": "You are a bot."}
    assert msgs[1] == {"role": "user", "content": "hello"}
    assert msgs[2] == {"role": "assistant", "content": "hi"}


def test_chat_empty_system_no_system_message():
    captured: dict[str, Any] = {}
    client = _capture_client(captured)
    llm = OllamaLLM(client=client)
    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi"),
    ]
    llm.chat(messages, tools=[], system="")
    body = captured["body"]
    assert "system" not in body
    assert body["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


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


def test_chat_captures_token_usage():
    body = {
        "message": {"role": "assistant", "content": "hi"},
        "prompt_eval_count": 15,
        "eval_count": 8,
        "prompt_eval_cached_count": 2,
        "total_duration": 500_000_000,
        "model": "llama3.2",
    }
    client = _make_client(httpx.Response(200, json=body))
    llm = OllamaLLM(base_url="http://ollama:11434", model="llama3.2", client=client)
    result = llm.chat([Message(role="user", content="hi")], tools=[])
    assert result.usage is not None
    assert result.usage.input_tokens == 15
    assert result.usage.output_tokens == 8
    assert result.usage.cached_tokens == 2
    assert result.usage.latency_ns == 500_000_000
    assert result.usage.model == "llama3.2"


def test_chat_usage_defaults_when_fields_missing():
    client = _make_client(httpx.Response(200, json={"message": {"content": "hi"}}))
    llm = OllamaLLM(client=client)
    result = llm.chat([Message(role="user", content="hi")], tools=[])
    assert result.usage is not None
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.usage.cached_tokens == 0
    assert result.usage.latency_ns == 0


def test_chat_null_token_fields_default_to_zero():
    body = {
        "message": {"content": "hi"},
        "prompt_eval_count": None,
        "eval_count": None,
        "total_duration": None,
        "model": None,
    }
    client = _make_client(httpx.Response(200, json=body))
    llm = OllamaLLM(model="llama3.2", client=client)
    result = llm.chat([Message(role="user", content="hi")], tools=[])
    assert result.usage is not None
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.usage.latency_ns == 0
    assert result.usage.model == "llama3.2"


def test_chat_usage_defaults_when_fields_missing2():
    client = _make_client(httpx.Response(200, json={"message": {"content": "hi"}}))
    llm = OllamaLLM(model="my-model", client=client)
    result = llm.chat([Message(role="user", content="hi")], tools=[])
    assert result.usage is not None
    assert result.usage.input_tokens == 0
    assert result.usage.model == "my-model"


def test_chat_with_tool_calls_still_captures_usage():
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
        "prompt_eval_count": 15,
        "eval_count": 8,
        "prompt_eval_cached_count": 2,
        "total_duration": 500_000_000,
        "model": "llama3.2",
    }
    client = _make_client(httpx.Response(200, json=body))
    llm = OllamaLLM(base_url="http://ollama:11434", model="llama3.2", client=client)
    result = llm.chat([Message(role="user", content="run")], tools=[])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "exec"
    assert result.usage is not None
    assert result.usage.input_tokens == 15
    assert result.usage.output_tokens == 8
    assert result.usage.cached_tokens == 2
    assert result.usage.latency_ns == 500_000_000
    assert result.usage.model == "llama3.2"


# --- Prompt-cache payload options (OLLAMA_NUM_CTX / OLLAMA_KEEP_ALIVE) -----


def test_chat_payload_num_ctx_when_env_set(monkeypatch):
    captured: dict[str, Any] = {}
    client = _capture_client(captured)
    monkeypatch.setenv("OLLAMA_NUM_CTX", "4096")
    llm = OllamaLLM(client=client)
    llm.chat([Message(role="user", content="hi")], tools=[])
    assert captured["body"]["options"]["num_ctx"] == 4096


def test_chat_payload_no_options_when_env_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)
    captured: dict[str, Any] = {}
    client = _capture_client(captured)
    llm = OllamaLLM(client=client)
    llm.chat([Message(role="user", content="hi")], tools=[])
    body = captured["body"]
    assert "options" not in body
    assert "keep_alive" not in body


def test_chat_payload_keep_alive_when_env_set(monkeypatch):
    captured: dict[str, Any] = {}
    client = _capture_client(captured)
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "30m")
    llm = OllamaLLM(client=client)
    llm.chat([Message(role="user", content="hi")], tools=[])
    assert captured["body"]["keep_alive"] == "30m"


def test_chat_ctor_param_overrides_env(monkeypatch):
    captured: dict[str, Any] = {}
    client = _capture_client(captured)
    monkeypatch.setenv("OLLAMA_NUM_CTX", "2048")
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "5m")
    llm = OllamaLLM(client=client, num_ctx=8192, keep_alive="1h")
    llm.chat([Message(role="user", content="hi")], tools=[])
    body = captured["body"]
    assert body["options"]["num_ctx"] == 8192
    assert body["keep_alive"] == "1h"


@pytest.mark.parametrize(
    ("raw", "warns"),
    [
        pytest.param("not-a-number", True, id="non-int"),
        pytest.param("0", True, id="zero"),
        pytest.param("-5", True, id="negative"),
        pytest.param("", False, id="empty"),
    ],
)
def test_chat_payload_invalid_num_ctx_env_ignored(monkeypatch, caplog, raw, warns):
    captured: dict[str, Any] = {}
    client = _capture_client(captured)
    monkeypatch.setenv("OLLAMA_NUM_CTX", raw)
    with caplog.at_level(logging.WARNING, logger="vektor.llm.ollama"):
        llm = OllamaLLM(client=client)
    llm.chat([Message(role="user", content="hi")], tools=[])
    assert "options" not in captured["body"]
    if warns:
        assert "Invalid OLLAMA_NUM_CTX" in caplog.text
    else:
        assert "Invalid OLLAMA_NUM_CTX" not in caplog.text


def test_chat_payload_empty_keep_alive_env_ignored(monkeypatch):
    captured: dict[str, Any] = {}
    client = _capture_client(captured)
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "")
    llm = OllamaLLM(client=client)
    llm.chat([Message(role="user", content="hi")], tools=[])
    assert "keep_alive" not in captured["body"]

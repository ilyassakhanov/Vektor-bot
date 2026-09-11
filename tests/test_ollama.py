"""Tests for the OllamaLLM provider using a mocked httpx.Client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from llm import LLMError
from llm.ollama import OllamaLLM


def _make_client(response: httpx.Response | Exception) -> httpx.Client:
    if isinstance(response, Exception):

        def handler(req: httpx.Request) -> httpx.Response:
            raise response
    else:

        def handler(req: httpx.Request) -> httpx.Response:
            return response

    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


def test_successful_ollama_request():
    client = _make_client(httpx.Response(200, json={"response": "Hello!"}))
    llm = OllamaLLM(base_url="http://ollama:11434", model="llama3.2", client=client)
    result = llm.generate("Hi")
    assert result.text == "Hello!"


def test_connection_failure():
    client = _make_client(httpx.ConnectError("refused"))
    llm = OllamaLLM(client=client)
    with pytest.raises(LLMError, match="Cannot connect"):
        llm.generate("Hi")


def test_timeout_failure():
    client = _make_client(httpx.TimeoutException("slow"))
    llm = OllamaLLM(client=client)
    with pytest.raises(LLMError, match="timed out"):
        llm.generate("Hi")


def test_http_error():
    client = _make_client(httpx.Response(500))
    llm = OllamaLLM(client=client)
    with pytest.raises(LLMError, match="LLM service error"):
        llm.generate("Hi")


def test_malformed_response():
    client = _make_client(httpx.Response(200, content=b"not-json"))
    llm = OllamaLLM(client=client)
    with pytest.raises(LLMError, match="Malformed"):
        llm.generate("Hi")


def test_empty_response_field():
    client = _make_client(httpx.Response(200, json={"response": ""}))
    llm = OllamaLLM(client=client)
    with pytest.raises(LLMError, match="Empty response"):
        llm.generate("Hi")


def test_request_uses_configured_base_url_and_model():
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["json"] = httpx.Response(200, json={"response": "ok"}).json()
        return httpx.Response(200, json={"response": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    llm = OllamaLLM(base_url="http://my-ollama:1234", model="my-model", client=client)
    llm.generate("ping")
    assert captured["url"] == "http://my-ollama:1234/api/generate"


def test_generate_captures_token_usage():
    body = {
        "response": "Hello!",
        "prompt_eval_count": 5,
        "eval_count": 3,
        "prompt_eval_cached_count": 1,
        "total_duration": 200_000_000,
        "model": "llama3.2",
    }
    client = _make_client(httpx.Response(200, json=body))
    llm = OllamaLLM(base_url="http://ollama:11434", model="llama3.2", client=client)
    result = llm.generate("Hi")
    assert result.text == "Hello!"
    assert result.usage is not None
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 3
    assert result.usage.cached_tokens == 1
    assert result.usage.latency_ns == 200_000_000
    assert result.usage.model == "llama3.2"


def test_generate_null_token_fields_default_to_zero():
    body = {
        "response": "Hello!",
        "prompt_eval_count": None,
        "eval_count": None,
        "total_duration": None,
        "model": None,
    }
    client = _make_client(httpx.Response(200, json=body))
    llm = OllamaLLM(base_url="http://ollama:11434", model="llama3.2", client=client)
    result = llm.generate("Hi")
    assert result.text == "Hello!"
    assert result.usage is not None
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.usage.latency_ns == 0
    assert result.usage.model == "llama3.2"


# --- Prompt-cache payload options (OLLAMA_NUM_CTX / OLLAMA_KEEP_ALIVE) -----


def test_generate_payload_includes_options_and_keep_alive(monkeypatch):
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"response": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    monkeypatch.setenv("OLLAMA_NUM_CTX", "4096")
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "30m")
    llm = OllamaLLM(client=client)
    llm.generate("Hi")
    body = captured["body"]
    assert body["options"]["num_ctx"] == 4096
    assert body["keep_alive"] == "30m"


def test_generate_payload_no_options_when_env_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"response": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    llm = OllamaLLM(client=client)
    llm.generate("Hi")
    body = captured["body"]
    assert "options" not in body
    assert "keep_alive" not in body

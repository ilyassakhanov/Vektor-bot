"""Tests for the OllamaLLM provider using a mocked httpx.Client."""

from __future__ import annotations

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

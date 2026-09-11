"""Tests for InstrumentedLLM — token and latency metrics recording.

Metrics live in the global REGISTRY and accumulate across tests, so each
test uses unique model label values and asserts before/after deltas.
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from llm.base import (
    LLM,
    ChatResponse,
    LLMError,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolSpec,
)
from llm.cost import estimate_cost
from llm.instrumented import InstrumentedLLM
from tests.fakes import FakeLLM, ScriptedLLM


def _tokens(model: str, direction: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "vektor_llm_tokens_total", {"model": model, "direction": direction}
        )
        or 0.0
    )


def _latency_count(model: str) -> float:
    return (
        REGISTRY.get_sample_value("vektor_llm_latency_seconds_count", {"model": model})
        or 0.0
    )


def _cost(model: str) -> float:
    return REGISTRY.get_sample_value("vektor_llm_cost_total", {"model": model}) or 0.0


class GenerateWithUsageLLM(LLM):
    """Minimal fake whose generate() returns a canned response with usage."""

    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    def generate(self, message: str) -> LLMResponse:
        return self._response

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system: str = "",
    ) -> ChatResponse:
        raise LLMError("chat is not supported by this fake")


def test_records_tokens_on_chat():
    inner = FakeLLM(
        chat_responses=[
            ChatResponse(
                content="x",
                usage=TokenUsage(
                    input_tokens=10,
                    output_tokens=5,
                    cached_tokens=2,
                    model="test-model-t4a",
                ),
            )
        ]
    )
    wrapped = InstrumentedLLM(inner)
    input_before = _tokens("test-model-t4a", "input")
    output_before = _tokens("test-model-t4a", "output")
    wrapped.chat([Message(role="user", content="hi")], [])
    assert _tokens("test-model-t4a", "input") - input_before == 10.0
    assert _tokens("test-model-t4a", "output") - output_before == 5.0


def test_records_cached_tokens_on_chat():
    inner = FakeLLM(
        chat_responses=[
            ChatResponse(
                content="x",
                usage=TokenUsage(
                    input_tokens=10,
                    output_tokens=5,
                    cached_tokens=5,
                    model="test-model-t1a",
                ),
            )
        ]
    )
    wrapped = InstrumentedLLM(inner)
    cached_before = _tokens("test-model-t1a", "cached")
    wrapped.chat([Message(role="user", content="hi")], [])
    assert _tokens("test-model-t1a", "cached") - cached_before == 5.0


def test_cached_tokens_zero_when_usage_has_none():
    inner = FakeLLM(
        chat_responses=[
            ChatResponse(
                content="x",
                usage=TokenUsage(
                    input_tokens=10, output_tokens=5, model="test-model-t1b"
                ),
            )
        ]
    )
    wrapped = InstrumentedLLM(inner)
    input_before = _tokens("test-model-t1b", "input")
    output_before = _tokens("test-model-t1b", "output")
    wrapped.chat([Message(role="user", content="hi")], [])
    cached_sample = REGISTRY.get_sample_value(
        "vektor_llm_tokens_total", {"model": "test-model-t1b", "direction": "cached"}
    )
    assert cached_sample is not None
    assert cached_sample == 0.0
    assert _tokens("test-model-t1b", "input") - input_before == 10.0
    assert _tokens("test-model-t1b", "output") - output_before == 5.0


def test_records_cost_on_chat():
    usage = TokenUsage(
        input_tokens=10, output_tokens=5, cached_tokens=2, model="llama3.2"
    )
    inner = FakeLLM(chat_responses=[ChatResponse(content="x", usage=usage)])
    wrapped = InstrumentedLLM(inner)
    cost_before = _cost("llama3.2")
    wrapped.chat([Message(role="user", content="hi")], [])
    assert _cost("llama3.2") - cost_before == estimate_cost(usage)


def test_records_latency_on_chat():
    inner = FakeLLM(
        chat_responses=[
            ChatResponse(content="x", usage=TokenUsage(model="test-model-t4b"))
        ]
    )
    wrapped = InstrumentedLLM(inner)
    before = _latency_count("test-model-t4b")
    wrapped.chat([Message(role="user", content="hi")], [])
    assert _latency_count("test-model-t4b") - before == 1.0


def test_no_usage_still_records_latency():
    inner = FakeLLM(chat_responses=[ChatResponse(content="x")])
    wrapped = InstrumentedLLM(inner)
    latency_before = _latency_count("unknown")
    input_before = _tokens("unknown", "input")
    output_before = _tokens("unknown", "output")
    wrapped.chat([Message(role="user", content="hi")], [])
    assert _latency_count("unknown") - latency_before == 1.0
    assert _tokens("unknown", "input") - input_before == 0.0
    assert _tokens("unknown", "output") - output_before == 0.0


def test_no_usage_no_cost_recorded():
    inner = FakeLLM(chat_responses=[ChatResponse(content="x")])
    wrapped = InstrumentedLLM(inner)
    cost_before = _cost("unknown")
    wrapped.chat([Message(role="user", content="hi")], [])
    assert _cost("unknown") - cost_before == 0.0


def test_generate_records_tokens():
    inner = GenerateWithUsageLLM(
        LLMResponse(
            text="x",
            usage=TokenUsage(input_tokens=7, output_tokens=3, model="test-model-t4d"),
        )
    )
    wrapped = InstrumentedLLM(inner)
    input_before = _tokens("test-model-t4d", "input")
    output_before = _tokens("test-model-t4d", "output")
    wrapped.generate("hi")
    assert _tokens("test-model-t4d", "input") - input_before == 7.0
    assert _tokens("test-model-t4d", "output") - output_before == 3.0


def test_generate_records_latency():
    inner = GenerateWithUsageLLM(
        LLMResponse(text="x", usage=TokenUsage(model="test-model-t4e"))
    )
    wrapped = InstrumentedLLM(inner)
    before = _latency_count("test-model-t4e")
    wrapped.generate("hi")
    assert _latency_count("test-model-t4e") - before == 1.0


def test_generate_records_cost():
    usage = TokenUsage(input_tokens=7, output_tokens=3, model="test-model-t4f")
    inner = GenerateWithUsageLLM(LLMResponse(text="x", usage=usage))
    wrapped = InstrumentedLLM(inner)
    cost_before = _cost("test-model-t4f")
    wrapped.generate("hi")
    assert _cost("test-model-t4f") - cost_before == estimate_cost(usage)


def test_usage_model_fallback_when_empty():
    inner = FakeLLM(
        chat_responses=[
            ChatResponse(
                content="x",
                usage=TokenUsage(input_tokens=4, output_tokens=2, model=""),
            )
        ]
    )
    wrapped = InstrumentedLLM(inner)
    input_before = _tokens("unknown", "input")
    output_before = _tokens("unknown", "output")
    wrapped.chat([Message(role="user", content="hi")], [])
    assert _tokens("unknown", "input") - input_before == 4.0
    assert _tokens("unknown", "output") - output_before == 2.0


def test_delegates_tool_calls():
    calls = [ToolCall(id="tc1", name="echo", arguments={"message": "hi"})]
    inner = ScriptedLLM([ChatResponse(content="", tool_calls=calls)])
    wrapped = InstrumentedLLM(inner)
    response = wrapped.chat([Message(role="user", content="go")], [])
    assert response.tool_calls == calls


def test_error_propagates_and_latency_recorded():
    inner = FakeLLM(error=LLMError("boom"))
    wrapped = InstrumentedLLM(inner)
    before = _latency_count("unknown")
    with pytest.raises(LLMError, match="boom"):
        wrapped.chat([Message(role="user", content="hi")], [])
    assert _latency_count("unknown") - before == 1.0


def test_chat_returns_response_unchanged():
    usage = TokenUsage(input_tokens=1, output_tokens=2, model="test-model-t4i")
    inner = FakeLLM(chat_responses=[ChatResponse(content="x", usage=usage)])
    wrapped = InstrumentedLLM(inner)
    response = wrapped.chat([Message(role="user", content="hi")], [])
    assert response.content == "x"
    assert response.usage == usage

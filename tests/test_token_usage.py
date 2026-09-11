"""Tests for TokenUsage dataclass and usage field on LLMResponse / ChatResponse."""

from __future__ import annotations

from llm.base import ChatResponse, LLMResponse, Message, TokenUsage


def test_token_usage_field_values():
    usage = TokenUsage(
        input_tokens=10,
        output_tokens=20,
        cached_tokens=2,
        model="llama3.2",
        latency_ns=1000,
    )
    assert usage.input_tokens == 10
    assert usage.output_tokens == 20
    assert usage.cached_tokens == 2
    assert usage.model == "llama3.2"
    assert usage.latency_ns == 1000


def test_token_usage_defaults():
    usage = TokenUsage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cached_tokens == 0
    assert usage.model == ""
    assert usage.latency_ns == 0


def test_llm_response_usage_defaults_to_none():
    r = LLMResponse(text="hi")
    assert r.usage is None


def test_llm_response_carries_usage():
    r = LLMResponse(text="hi", usage=TokenUsage(input_tokens=1))
    assert r.usage is not None
    assert r.usage.input_tokens == 1


def test_chat_response_usage_defaults_to_none():
    cr = ChatResponse(content="hi")
    assert cr.usage is None


def test_chat_response_carries_usage():
    cr = ChatResponse(content="hi", usage=TokenUsage())
    assert cr.usage is not None


def test_fake_llm_chat_returns_usage_none():
    from tests.fakes import FakeLLM

    llm = FakeLLM()
    result = llm.chat([Message(role="user", content="hi")], tools=[])
    assert result.usage is None


def test_scripted_llm_chat_works_unchanged():
    from tests.fakes import ScriptedLLM

    llm = ScriptedLLM([ChatResponse(content="x")])
    result = llm.chat([Message(role="user", content="hi")], tools=[])
    assert result.content == "x"

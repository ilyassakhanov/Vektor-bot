"""Tests for the notional token-cost model (llm/cost.py).

Prices are notional (a local Ollama instance costs $0 in reality); these
tests pin the default table ($0.35 in / $1.25 out per 1M), the
cached-tokens-are-free-input rule, and env-override behavior.
"""

from __future__ import annotations

import pytest

from llm.base import TokenUsage
from llm.cost import estimate_cost

PRICE_IN_PER_1M = 0.35
PRICE_OUT_PER_1M = 1.25


@pytest.fixture(autouse=True)
def _no_price_env(monkeypatch):
    monkeypatch.delenv("LLM_PRICE_IN_PER_1M", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUT_PER_1M", raising=False)


def test_estimate_cost_pricing_math():
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=500_000, model="llama3.2")
    assert estimate_cost(usage) == pytest.approx(0.35 + 0.625)


def test_estimate_cost_cached_tokens_are_free_input():
    usage = TokenUsage(input_tokens=100, cached_tokens=40, model="llama3.2")
    assert estimate_cost(usage) == pytest.approx(60 * PRICE_IN_PER_1M / 1e6)


def test_estimate_cost_env_override(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_IN_PER_1M", "1.0")
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=0, model="llama3.2")
    assert estimate_cost(usage) == pytest.approx(1.0)
    assert estimate_cost(usage) != pytest.approx(PRICE_IN_PER_1M)


def test_estimate_cost_unknown_model_uses_default():
    usage = TokenUsage(
        input_tokens=1_000_000, output_tokens=1_000_000, model="never-heard-of"
    )
    assert estimate_cost(usage) == pytest.approx(PRICE_IN_PER_1M + PRICE_OUT_PER_1M)


def test_estimate_cost_zero_usage():
    assert estimate_cost(TokenUsage()) == 0.0


def test_estimate_cost_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_IN_PER_1M", "abc")
    monkeypatch.setenv("LLM_PRICE_OUT_PER_1M", "abc")
    usage = TokenUsage(
        input_tokens=1_000_000, output_tokens=1_000_000, model="llama3.2"
    )
    assert estimate_cost(usage) == pytest.approx(PRICE_IN_PER_1M + PRICE_OUT_PER_1M)


def test_estimate_cost_negative_env_price_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_IN_PER_1M", "-1.0")
    usage = TokenUsage(input_tokens=1_000_000, model="llama3.2")
    assert estimate_cost(usage) == pytest.approx(PRICE_IN_PER_1M)


def test_estimate_cost_nan_env_price_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_OUT_PER_1M", "nan")
    usage = TokenUsage(output_tokens=1_000_000, model="llama3.2")
    assert estimate_cost(usage) == pytest.approx(PRICE_OUT_PER_1M)


def test_estimate_cost_inf_env_price_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_IN_PER_1M", "inf")
    usage = TokenUsage(input_tokens=1_000_000, model="llama3.2")
    assert estimate_cost(usage) == pytest.approx(PRICE_IN_PER_1M)

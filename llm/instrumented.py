"""Instrumented LLM wrapper — records token and latency metrics."""

from __future__ import annotations

import logging
import time
from typing import Any

import metrics
from llm.base import (
    LLM,
    ChatResponse,
    LLMResponse,
    Message,
    TokenUsage,
    ToolSpec,
)
from llm.cost import estimate_cost

log = logging.getLogger("vektor.llm.instrumented")

_UNKNOWN_MODEL = "unknown"


class InstrumentedLLM(LLM):
    """Wraps an LLM provider and records token/latency metrics."""

    def __init__(self, inner: LLM) -> None:
        self._inner = inner

    def generate(self, message: str) -> LLMResponse:
        model = _UNKNOWN_MODEL
        start = time.perf_counter()
        try:
            response = self._inner.generate(message)
            model = self._resolve_model(response.usage)
            self._record_tokens(model, response.usage)
            return response
        finally:
            self._observe_latency(model, start)

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system: str = "",
    ) -> ChatResponse:
        model = _UNKNOWN_MODEL
        start = time.perf_counter()
        try:
            response = self._inner.chat(messages, tools, system=system)
            model = self._resolve_model(response.usage)
            self._record_tokens(model, response.usage)
            return response
        finally:
            self._observe_latency(model, start)

    def close(self) -> None:
        close: Any = getattr(self._inner, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _resolve_model(usage: TokenUsage | None) -> str:
        if usage is None:
            return _UNKNOWN_MODEL
        return usage.model or _UNKNOWN_MODEL

    @staticmethod
    def _record_tokens(model: str, usage: TokenUsage | None) -> None:
        if usage is None:
            return
        metrics.llm_tokens_total.labels(model=model, direction="input").inc(
            usage.input_tokens
        )
        metrics.llm_tokens_total.labels(model=model, direction="output").inc(
            usage.output_tokens
        )
        metrics.llm_tokens_total.labels(model=model, direction="cached").inc(
            usage.cached_tokens
        )
        metrics.llm_cost_total.labels(model=model).inc(estimate_cost(usage))

    @staticmethod
    def _observe_latency(model: str, start: float) -> None:
        metrics.llm_latency_seconds.labels(model=model).observe(
            time.perf_counter() - start
        )

"""Shared test utilities."""

from __future__ import annotations

from llm.base import LLM, LLMError, LLMResponse


class FakeLLM(LLM):
    """In-memory LLM for tests — returns a canned reply or raises."""

    def __init__(self, reply: str = "ok", *, error: LLMError | None = None) -> None:
        self._reply = reply
        self._error = error
        self.calls: list[str] = []

    def generate(self, message: str) -> LLMResponse:
        self.calls.append(message)
        if self._error is not None:
            raise self._error
        return LLMResponse(text=self._reply)

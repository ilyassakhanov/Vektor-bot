"""Shared test utilities."""

from __future__ import annotations

from collections.abc import Sequence

from llm.base import (
    LLM,
    ChatResponse,
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)


class FakeLLM(LLM):
    """In-memory LLM for tests — returns a canned reply or raises.

    For ``generate()``: returns ``reply`` or raises ``error``.
    For ``chat()``: returns the next :class:`ChatResponse` from
    ``chat_responses`` (a list of pre-seeded responses). Each call pops one
    response from the front. If ``chat_responses`` is empty, returns a
    simple final answer.
    """

    def __init__(
        self,
        reply: str = "ok",
        *,
        error: LLMError | None = None,
        chat_responses: Sequence[ChatResponse] | None = None,
    ) -> None:
        self._reply = reply
        self._error = error
        self.calls: list[str] = []
        self.chat_calls: list[tuple[list[Message], list[ToolSpec], str]] = []
        self._chat_responses: list[ChatResponse] = (
            list(chat_responses) if chat_responses else []
        )

    def generate(self, message: str) -> LLMResponse:
        self.calls.append(message)
        if self._error is not None:
            raise self._error
        return LLMResponse(text=self._reply)

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system: str = "",
    ) -> ChatResponse:
        self.chat_calls.append((list(messages), list(tools), system))
        if self._error is not None:
            raise self._error
        if self._chat_responses:
            return self._chat_responses.pop(0)
        return ChatResponse(content=self._reply)


class ScriptedLLM(LLM):
    """LLM that plays back a pre-seeded list of :class:`ChatResponse`.

    Each ``chat()`` call pops one response. Raises ``IndexError`` if the
    script is exhausted — this makes it obvious in tests if the agent loop
    iterates more than expected.
    """

    def __init__(self, script: Sequence[ChatResponse]) -> None:
        self._script: list[ChatResponse] = list(script)
        self.chat_calls: list[tuple[list[Message], list[ToolSpec], str]] = []
        self.generate_calls: list[str] = []

    def generate(self, message: str) -> LLMResponse:
        self.generate_calls.append(message)
        if not self._script:
            return LLMResponse(text="ok")
        r = self._script.pop(0)
        return LLMResponse(text=r.content)

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system: str = "",
    ) -> ChatResponse:
        self.chat_calls.append((list(messages), list(tools), system))
        if not self._script:
            raise IndexError("ScriptedLLM script exhausted")
        return self._script.pop(0)


def make_tool_call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)

"""Abstract LLM interface used by the Telegram bot and Agent.

The bot and agent depend only on :class:`LLM`. Provider-specific code lives
in ``llm/<provider>.py`` and must not leak into this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(Exception):
    """Application-level error raised by any LLM provider."""


@dataclass(frozen=True)
class LLMResponse:
    """Provider-agnostic response returned by :meth:`LLM.generate`."""

    text: str


# --- Conversation / tool-call types -----------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """Description of a tool offered to the LLM (JSON-schema parameters)."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """Result of executing a tool, fed back into the conversation."""

    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    """A single conversation message (user, assistant, or tool result)."""

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ChatResponse:
    """Response from :meth:`LLM.chat` — may contain tool calls or a final answer."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLM(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    def generate(self, message: str) -> LLMResponse:
        """Generate a response for ``message`` (simple single-turn interface).

        Raises:
            LLMError: if generation fails for any provider-specific reason.
        """

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system: str = "",
    ) -> ChatResponse:
        """Multi-turn chat with optional tool definitions.

        Args:
            messages: Full conversation history (user / assistant / tool).
            tools: Available tool specifications.
            system: System prompt (may include loaded skill instructions).

        Returns:
            A :class:`ChatResponse` — either a final text answer or one or
            more :class:`ToolCall` requests.

        Raises:
            LLMError: if the request fails for any provider-specific reason.
        """

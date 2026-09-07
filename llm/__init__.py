"""LLM integration layer with a provider-independent interface."""

from __future__ import annotations

from llm.base import (
    LLM,
    ChatResponse,
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
)

__all__ = [
    "LLM",
    "ChatResponse",
    "LLMError",
    "LLMResponse",
    "Message",
    "OllamaLLM",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
]


def __getattr__(name: str) -> type:
    if name == "OllamaLLM":
        from llm.ollama import OllamaLLM

        return OllamaLLM
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

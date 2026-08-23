"""LLM integration layer with a provider-independent interface."""

from __future__ import annotations

from llm.base import LLM, LLMError, LLMResponse

__all__ = ["LLM", "LLMError", "LLMResponse", "OllamaLLM"]


def __getattr__(name: str) -> object:
    if name == "OllamaLLM":
        from llm.ollama import OllamaLLM

        return OllamaLLM
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

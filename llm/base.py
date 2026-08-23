"""Abstract LLM interface used by the Telegram bot.

The bot depends only on :class:`LLM`. Provider-specific code lives in
``llm/<provider>.py`` and must not leak into this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class LLMError(Exception):
    """Application-level error raised by any LLM provider."""


@dataclass(frozen=True)
class LLMResponse:
    """Provider-agnostic response returned by :meth:`LLM.generate`."""

    text: str


class LLM(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    def generate(self, message: str) -> LLMResponse:
        """Generate a response for ``message``.

        Raises:
            LLMError: if generation fails for any provider-specific reason.
        """

"""Abstract Tool interface — provider-agnostic tool definition.

The Agent invokes tools through :class:`ToolRegistry`, never directly.
Adding a new tool requires only implementing :class:`Tool` and registering
it — the agent loop does not change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ToolError(Exception):
    """Error raised by a tool — caught by the registry, returned to the LLM."""


class Tool(ABC):
    """Abstract base class for agent tools.

    A tool has a name, description, JSON-schema parameters, and an
    ``execute`` method. The registry calls ``execute(**arguments)`` and
    converts any :class:`ToolError` into an error string for the LLM.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name used by the LLM to invoke this tool."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what the tool does."""

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON-schema dict describing the tool's parameters."""

    @abstractmethod
    def execute(self, **kwargs: Any) -> str:
        """Execute the tool with the given arguments and return a string result.

        Raises:
            ToolError: if execution fails (the registry catches this).
        """

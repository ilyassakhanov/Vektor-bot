"""Tool registry — the agent's only interface to tools.

The agent loop calls ``registry.execute(name, **arguments)`` and
``registry.specs()`` — it never imports individual tool implementations.
"""

from __future__ import annotations

import logging
from typing import Any

from llm.base import ToolSpec
from tools.base import Tool, ToolError

log = logging.getLogger("vektor.tools.registry")


class ToolRegistry:
    """Registry of available tools.

    Tools are registered by name. The registry exposes their specs to the
    LLM and executes them by name. Tool failures are caught and returned
    as error strings so the bot never crashes.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        log.info("Registered tool: %s", tool.name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=t.name,
                description=t.description,
                parameters=t.parameters,
            )
            for t in self._tools.values()
        ]

    def execute(self, name: str, **arguments: Any) -> str:
        tool = self._tools.get(name)
        if tool is None:
            msg = f"Error: tool '{name}' not found."
            log.warning(msg)
            return msg
        try:
            return tool.execute(**arguments)
        except ToolError as exc:
            msg = f"Error: tool '{name}' failed: {exc}"
            log.warning(msg)
            return msg
        except Exception as exc:
            msg = f"Error: tool '{name}' raised: {exc}"
            log.exception(msg)
            return msg

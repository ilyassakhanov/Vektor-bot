"""Agent — bounded agentic loop.

The agent receives a user message, calls the LLM with conversation history,
tools, and skills. If the LLM requests tools, the agent executes them via the
ToolRegistry, feeds results back, and repeats until the LLM returns a final
answer or the max iteration limit is reached.

The agent depends only on the LLM interface — never on a concrete provider.
"""

from __future__ import annotations

import logging

from llm.base import LLM, Message
from tools.registry import ToolRegistry

log = logging.getLogger("vektor.agent")

_DEFAULT_MAX_ITERATIONS = 8


class Agent:
    """Bounded agentic loop with tool and skill support.

    Args:
        llm: The LLM provider (must implement the ``chat()`` interface).
        tools: ToolRegistry with available tools.
        system_prompt: System prompt (may include loaded skill instructions).
        max_iterations: Maximum number of LLM calls before stopping.
    """

    def __init__(
        self,
        llm: LLM,
        tools: ToolRegistry,
        system_prompt: str = "",
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._system = system_prompt
        self._max_iterations = max_iterations

    def run(self, user_message: str, messages: list[Message] | None = None) -> str:
        """Process a user message through the agentic loop.

        Args:
            user_message: The user's input text.
            messages: Prior conversation history (appended in place).
                If None, a fresh conversation is started.

        Returns the LLM's final text answer. Raises ``LLMError`` if the LLM
        itself fails.
        """
        if messages is None:
            messages = []
        messages.append(Message(role="user", content=user_message))
        tool_specs = self._tools.specs()

        for iteration in range(self._max_iterations):
            log.debug("iteration %d/%d", iteration + 1, self._max_iterations)
            response = self._llm.chat(messages, tool_specs, system=self._system)

            if not response.tool_calls:
                log.info("LLM returned final answer at iteration %d", iteration + 1)
                messages.append(Message(role="assistant", content=response.content))
                return response.content

            assistant_msg = Message(
                role="assistant",
                content=response.content,
                tool_calls=list(response.tool_calls),
            )
            messages.append(assistant_msg)

            for tc in response.tool_calls:
                result = self._tools.execute(tc.name, **tc.arguments)
                log.info("tool %s result: %s", tc.name, result[:200])
                tool_msg = Message(
                    role="tool",
                    content=result,
                    tool_call_id=tc.id,
                )
                messages.append(tool_msg)

        log.warning("Max iterations (%d) reached", self._max_iterations)
        msg = (
            "I reached the maximum number of iterations"
            f" ({self._max_iterations}) without producing a final answer."
        )
        messages.append(Message(role="assistant", content=msg))
        return msg

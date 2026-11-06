"""Agent — bounded agentic loop.

The agent receives a user message, calls the LLM with conversation history,
tools, and skills. If the LLM requests tools, the agent executes them via the
ToolRegistry, feeds results back, and repeats until the LLM returns a final
answer or the max iteration limit is reached.

The agent depends only on the LLM interface — never on a concrete provider.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import metrics
from llm.base import LLM, Message
from tools.registry import ToolRegistry

log = logging.getLogger("vektor.agent")

_DEFAULT_MAX_ITERATIONS = 8
DEFAULT_MAX_TOOL_CALLS_PER_TURN = 4

SKIPPED_BUDGET_RESULT = "skipped: run budget exceeded"
SKIPPED_TOO_MANY_RESULT = (
    "skipped: too many tool calls in one turn (max {max_calls}); "
    "request fewer calls at a time"
)

MAX_ITERATIONS_REPLY = (
    "I reached the maximum number of iterations"
    " ({max_iterations}) without producing a final answer."
)


BUDGET_EXCEEDED_REPLY = (
    "I stopped because this request took longer than the allowed {max_run_seconds}s."
)


class Agent:
    """Bounded agentic loop with tool and skill support.

    Args:
        llm: The LLM provider (must implement the ``chat()`` interface).
        tools: ToolRegistry with available tools.
        system_prompt: System prompt (may include loaded skill instructions).
        max_iterations: Maximum number of LLM calls before stopping.
        max_run_seconds: Wall-clock budget per run, checked before every
            iteration after the first and before every tool call (``None`` or ``<= 0`` = no budget). It
            cannot interrupt an in-flight LLM call or tool, so a run can
            overrun it by at most one iteration.
        max_tool_calls_per_turn: Maximum tool calls executed from one LLM
            reply (default 4; ``None`` = no cap). Extra calls get a
            ``skipped`` result so every call id is still answered.
        clock: Monotonic time source (injectable for tests).
    """

    def __init__(
        self,
        llm: LLM,
        tools: ToolRegistry,
        system_prompt: str = "",
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        max_run_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_tool_calls_per_turn: int | None = DEFAULT_MAX_TOOL_CALLS_PER_TURN,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._system = system_prompt
        self._max_iterations = max_iterations
        self._max_run_seconds = max_run_seconds
        self._clock = clock
        self._max_tool_calls_per_turn = max_tool_calls_per_turn

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
        budget = self._max_run_seconds
        if budget is not None and budget <= 0:
            budget = None
        start = self._clock() if budget is not None else 0.0

        for iteration in range(self._max_iterations):
            if budget is not None and iteration > 0 and self._clock() - start >= budget:
                return self._budget_exceeded(messages, iteration, budget)
            log.debug("iteration %d/%d", iteration + 1, self._max_iterations)
            response = self._llm.chat(messages, tool_specs, system=self._system)

            if not response.tool_calls:
                log.info("LLM returned final answer at iteration %d", iteration + 1)
                messages.append(Message(role="assistant", content=response.content))
                metrics.agent_iterations.observe(iteration + 1)
                return response.content

            assistant_msg = Message(
                role="assistant",
                content=response.content,
                tool_calls=list(response.tool_calls),
            )
            messages.append(assistant_msg)

            cap = self._max_tool_calls_per_turn
            over_budget = False
            for index, tc in enumerate(response.tool_calls):
                if not over_budget and budget is not None:
                    over_budget = self._clock() - start >= budget
                if over_budget:
                    result = SKIPPED_BUDGET_RESULT
                elif cap is not None and index >= cap:
                    result = SKIPPED_TOO_MANY_RESULT.format(max_calls=cap)
                    log.warning("tool %s skipped: over %d calls per turn", tc.name, cap)
                else:
                    result = self._tools.execute(tc.name, **tc.arguments)
                    log.info("tool %s executed", tc.name)
                # One result per call id keeps the history valid for the LLM.
                messages.append(
                    Message(role="tool", content=result, tool_call_id=tc.id)
                )
            if over_budget and budget is not None:
                return self._budget_exceeded(messages, iteration + 1, budget)

        log.warning("Max iterations (%d) reached", self._max_iterations)
        metrics.agent_iterations.observe(self._max_iterations)
        metrics.agent_max_iterations_reached_total.inc()
        msg = MAX_ITERATIONS_REPLY.format(max_iterations=self._max_iterations)
        messages.append(Message(role="assistant", content=msg))
        return msg

    def _budget_exceeded(
        self, messages: list[Message], iterations: int, budget: float
    ) -> str:
        log.warning("Run budget (%gs) exceeded after %d iterations", budget, iterations)
        metrics.agent_iterations.observe(iterations)
        metrics.agent_budget_exceeded_total.inc()
        msg = BUDGET_EXCEEDED_REPLY.format(max_run_seconds=format(budget, "g"))
        messages.append(Message(role="assistant", content=msg))
        return msg

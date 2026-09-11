"""Conversation manager — per-chat context with /new support.

Each Telegram chat is one continuous conversation. This maintains a
``chat_id → messages`` mapping in memory. The ``/new`` command clears
only the current chat's context and does not send anything to the LLM.
Each chat's history is capped at ``max_messages`` (env
``CONVERSATION_MAX_MESSAGES``, default 12): after every agent run the
oldest messages are dropped, the latest user message is never dropped,
and the first kept message always has role ``user``.
"""

from __future__ import annotations

import logging
import os

from agent.agent import Agent
from llm.base import Message

log = logging.getLogger("vektor.agent.conversation")

_NEW_COMMAND = "/new"
_NEW_REPLY = "Started a new conversation. Previous context cleared."
_DEFAULT_MAX_MESSAGES = 12
_ENV_MAX_MESSAGES = "CONVERSATION_MAX_MESSAGES"


def _max_messages_from_env() -> int:
    """Read ``CONVERSATION_MAX_MESSAGES`` — falls back to the default on invalid values."""
    raw = os.environ.get(_ENV_MAX_MESSAGES)
    if raw is None:
        return _DEFAULT_MAX_MESSAGES
    value: int | None
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or value < 1:
        log.warning(
            "Invalid %s %r, using default %d",
            _ENV_MAX_MESSAGES,
            raw,
            _DEFAULT_MAX_MESSAGES,
        )
        return _DEFAULT_MAX_MESSAGES
    return value


class ConversationManager:
    """Manages per-chat conversation context and dispatches to the Agent.

    Args:
        agent: The agent to run for each message.
        max_messages: Maximum number of messages kept per chat after each
            run (the newest are kept). When None, read from the
            ``CONVERSATION_MAX_MESSAGES`` environment variable (default 12).
    """

    def __init__(self, agent: Agent, max_messages: int | None = None) -> None:
        self._agent = agent
        if max_messages is None:
            max_messages = _max_messages_from_env()
        self._max_messages = max_messages
        self._chats: dict[int | str, list[Message]] = {}

    def handle(self, chat_id: int | str, text: str) -> str:
        """Handle a message from a chat.

        If the message is ``/new``, clears the chat's context and returns
        a confirmation — the LLM is not called.

        Otherwise, appends the user message to the chat's history, runs
        the agent, trims the history to ``max_messages``, and returns the
        response.
        """
        if text.strip() == _NEW_COMMAND:
            self._chats.pop(chat_id, None)
            log.info("Chat %s context cleared", chat_id)
            return _NEW_REPLY

        messages = self._chats.setdefault(chat_id, [])
        reply = self._agent.run(text, messages)
        self._trim(chat_id, messages)
        return reply

    def _trim(self, chat_id: int | str, messages: list[Message]) -> None:
        """Cap the chat history in place, keeping the newest messages.

        Keeps at most ``max_messages`` messages. When the newest window
        would exclude the latest user message, the window is extended
        back to it instead. Leading orphan assistant/tool messages are
        dropped so the first kept message always has role ``user`` —
        this preserves assistant-tool_call → tool-result pairing.
        """
        if len(messages) <= self._max_messages:
            return
        latest_user_idx = max(
            (i for i, m in enumerate(messages) if m.role == "user"), default=-1
        )
        if latest_user_idx < 0:
            return
        start = min(len(messages) - self._max_messages, latest_user_idx)
        while messages[start].role != "user":
            start += 1
        if start > 0:
            original_len = len(messages)
            del messages[:start]
            log.debug(
                "Chat %s history trimmed from %d to %d messages",
                chat_id,
                original_len,
                len(messages),
            )

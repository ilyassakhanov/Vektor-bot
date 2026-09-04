"""Conversation manager — per-chat context with /new support.

Each Telegram chat is one continuous conversation. This maintains a
``chat_id → messages`` mapping in memory. The ``/new`` command clears
only the current chat's context and does not send anything to the LLM.
"""

from __future__ import annotations

import logging

from agent.agent import Agent
from llm.base import Message

log = logging.getLogger("vektor.agent.conversation")

_NEW_COMMAND = "/new"
_NEW_REPLY = "Started a new conversation. Previous context cleared."


class ConversationManager:
    """Manages per-chat conversation context and dispatches to the Agent.

    Args:
        agent: The agent to run for each message.
    """

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._chats: dict[int | str, list[Message]] = {}

    def handle(self, chat_id: int | str, text: str) -> str:
        """Handle a message from a chat.

        If the message is ``/new``, clears the chat's context and returns
        a confirmation — the LLM is not called.

        Otherwise, appends the user message to the chat's history, runs
        the agent, and returns the response.
        """
        if text.strip() == _NEW_COMMAND:
            self._chats.pop(chat_id, None)
            log.info("Chat %s context cleared", chat_id)
            return _NEW_REPLY

        messages = self._chats.setdefault(chat_id, [])
        return self._agent.run(text, messages)

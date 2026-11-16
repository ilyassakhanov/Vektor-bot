"""Conversation manager — per-chat context with /new support.

Each Telegram chat is one continuous conversation. This maintains a
``chat_id → messages`` mapping in memory. The ``/new`` command clears
only the current chat's context and does not send anything to the LLM.
Each chat's history is capped at ``max_messages`` (env
``CONVERSATION_MAX_MESSAGES``, default 12): after every agent run the
oldest messages are dropped, the latest user message is never dropped,
and the first kept message always has role ``user``.

Runs never overlap within a chat, and callers never wait: a chat with a
run in flight is marked busy for the whole agent run plus the trim, and a
second message (``/new`` included) for that chat raises ``ChatBusyError``
immediately instead of blocking the caller's worker thread. Different chats
run in parallel. Only chats with a run in flight are tracked, so the busy
set never grows beyond the number of concurrent runs.

A caller that must do work before the agent turn (the document upload:
download, extraction, ingestion) takes the busy mark up front with
``reserve(chat_id)`` and runs the turn inside it via
``handle(chat_id, text, reservation=...)``, so the chat stays busy for the
whole block and a busy chat is detected before any of that work starts.
``handle`` is the single public turn entry point for both paths, so a
subclass or wrapper overriding it sees every turn.

A failed agent run (any exception) rolls the chat history back to its
pre-turn state — the user message and any partial assistant/tool messages
the run appended are removed — so failures never accumulate orphan user
messages; the history is trimmed after every run, successful or not.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from agent.agent import Agent
from config import positive_env
from llm.base import Message

log = logging.getLogger("vektor.agent.conversation")

NEW_COMMAND = "/new"
_NEW_REPLY = "Started a new conversation. Previous context cleared."
_DEFAULT_MAX_MESSAGES = 12
_ENV_MAX_MESSAGES = "CONVERSATION_MAX_MESSAGES"


def _max_messages_from_env() -> int:
    """Read ``CONVERSATION_MAX_MESSAGES`` — falls back to the default on invalid values."""
    return positive_env(_ENV_MAX_MESSAGES, _DEFAULT_MAX_MESSAGES, int)


class ChatBusyError(Exception):
    """A run is already in flight for this chat; the message was not processed.

    Raised by ``ConversationManager.handle`` without touching the history or
    calling the LLM. Callers map it to a busy reply.
    """

    def __init__(self, chat_id: int | str) -> None:
        super().__init__(f"chat {chat_id} has a run in flight")
        self.chat_id = chat_id


class ChatReservation:
    """Proof that a chat's busy mark is held.

    Obtained only from ``ConversationManager.reserve`` and passed to
    ``ConversationManager.handle(chat_id, text, reservation=...)`` to run
    turns inside the held mark; unusable after the ``with`` block that
    created it has exited.
    """

    def __init__(self, conv: ConversationManager, chat_id: int | str) -> None:
        self._conv = conv
        self.chat_id = chat_id
        self._active = True

    def _close(self) -> None:
        self._active = False


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
        # Chats with a run in flight. The guard protects only the set and is
        # never held across a run; entries are removed in ``finally``.
        self._in_flight: set[int | str] = set()
        self._in_flight_guard = threading.Lock()

    def _try_mark_busy(self, chat_id: int | str) -> bool:
        with self._in_flight_guard:
            if chat_id in self._in_flight:
                return False
            self._in_flight.add(chat_id)
            return True

    def _unmark_busy(self, chat_id: int | str) -> None:
        with self._in_flight_guard:
            self._in_flight.discard(chat_id)

    @contextmanager
    def reserve(self, chat_id: int | str) -> Iterator[ChatReservation]:
        """Hold the chat's busy mark for the whole ``with`` block.

        Raises ``ChatBusyError`` immediately (never waits) if a run is
        already in flight for this chat. The mark is released when the block
        exits, on any exception included.
        """
        if not self._try_mark_busy(chat_id):
            log.info("Chat %s busy: run already in flight", chat_id)
            raise ChatBusyError(chat_id)
        reservation = ChatReservation(self, chat_id)
        try:
            yield reservation
        finally:
            reservation._close()
            self._unmark_busy(chat_id)

    def handle(
        self,
        chat_id: int | str,
        text: str,
        *,
        reservation: ChatReservation | None = None,
    ) -> str:
        """Handle a message from a chat.

        If the message is ``/new``, clears the chat's context and returns
        a confirmation — the LLM is not called.

        Otherwise, appends the user message to the chat's history, runs
        the agent, trims the history to ``max_messages``, and returns the
        response.

        Never blocks: if a run is already in flight for this chat, raises
        ``ChatBusyError`` immediately (for ``/new`` too) — no LLM call, the
        history is untouched. The busy mark covers the run and the trim and
        is cleared on any exception. Different chats never block each other.

        With ``reservation`` (from ``reserve(chat_id)``), the turn runs inside
        that already-held busy mark instead of taking a new one. A reservation
        that is no longer held, belongs to another manager, or is for another
        chat raises ``RuntimeError``/``ValueError``.

        If the agent run raises, the history is rolled back to its pre-turn
        state and the exception propagates.
        """
        if reservation is None:
            with self.reserve(chat_id) as held:
                return self._handle_reserved(chat_id, text, held)
        return self._handle_reserved(chat_id, text, reservation)

    def _handle_reserved(
        self, chat_id: int | str, text: str, reservation: ChatReservation
    ) -> str:
        """One turn for a chat whose busy mark ``reservation`` holds."""
        if not reservation._active:
            raise RuntimeError("chat reservation is no longer held")
        if reservation._conv is not self or reservation.chat_id != chat_id:
            raise ValueError("chat reservation does not match this chat")
        if text.strip() == NEW_COMMAND:
            self._chats.pop(chat_id, None)
            log.info("Chat %s context cleared", chat_id)
            return _NEW_REPLY

        messages = self._chats.setdefault(chat_id, [])
        pre_turn_len = len(messages)
        try:
            return self._agent.run(text, messages)
        except BaseException:
            # Agent.run appends in place (user message first, then any
            # assistant/tool messages); drop everything this turn added.
            del messages[pre_turn_len:]
            log.info("Chat %s turn failed; history rolled back", chat_id)
            raise
        finally:
            self._trim(chat_id, messages)

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

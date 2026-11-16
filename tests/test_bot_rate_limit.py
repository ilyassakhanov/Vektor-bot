"""Handler-level tests for per-user rate limiting and worker-thread config.

Offline: FakeLLM/ScriptedLLM, a fake TeleBot for uploads, a tmp SQLite kb.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY
from telebot.apihelper import ApiTelegramException

import bot as bot_module
from agent.agent import Agent
from agent.conversation import ConversationManager
from agent.rate_limit import RateLimiter
from bot import (
    _BUSY_REPLY,
    _CHAT_BUSY_REPLY,
    _DOCUMENT_BUSY_REPLY,
    _DOCUMENT_READY_REPLY,
    _DOCUMENT_RECEIVED_REPLY,
    _EMBEDDING_REPLY,
    _EXTRACTING_REPLY,
    _RATE_LIMITED_REPLY,
    _worker_threads_from_env,
    build_document_handler,
    build_rate_limiter,
    create_bot,
    handle_message,
)
from llm import LLMError
from llm.base import ChatResponse
from retrieval.embeddings import EmbeddingError
from tests.fakes import FakeLLM, ScriptedLLM
from tests.test_bot_documents import (
    FakeBot,
    FakeEmbedder,
    _make_doc_message,
    _make_kb,
)
from tools.registry import ToolRegistry


def _conv(llm) -> ConversationManager:
    return ConversationManager(Agent(llm, ToolRegistry()))


def _msg(text: str, user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=42 + user_id, type="private"),
        from_user=SimpleNamespace(id=user_id, username="tester", first_name="T"),
        text=text,
    )


def _send(
    message: SimpleNamespace,
    conv: ConversationManager,
    limiter: RateLimiter | None,
    allowed: frozenset[str] | None = None,
    document_handler=None,
) -> list[str]:
    replies: list[str] = []
    handle_message(
        message,
        conv,
        lambda _m, text: replies.append(text),
        allowed,
        document_handler,
        rate_limiter=limiter,
    )
    return replies


def _chat_busy(path: str) -> float:
    return REGISTRY.get_sample_value("vektor_chat_busy_total", {"path": path}) or 0.0


def _limited(reason: str) -> float:
    return (
        REGISTRY.get_sample_value("vektor_rate_limited_total", {"reason": reason})
        or 0.0
    )


# --- in-flight -----------------------------------------------------------------


def test_in_flight_user_gets_one_busy_reply_no_llm_call_history_untouched() -> None:
    llm = ScriptedLLM([ChatResponse(content="first"), ChatResponse(content="second")])
    conv = _conv(llm)
    limiter = RateLimiter(max_per_window=100, window_s=60.0)
    assert _send(_msg("hello"), conv, limiter) == ["first"]

    assert limiter.try_acquire("1").ok  # simulate a run still in flight
    before = _limited("in_flight")
    assert _send(_msg("ignored"), conv, limiter) == [_BUSY_REPLY]
    assert _limited("in_flight") == before + 1
    assert len(llm.chat_calls) == 1
    limiter.release("1")

    assert _send(_msg("again"), conv, limiter) == ["second"]
    contents = [m.content for m in llm.chat_calls[1][0]]
    assert contents == ["hello", "first", "again"]


# --- chat in flight (another user's run in the same group chat) ------------------


def _group_msg(text: str, user_id: int) -> SimpleNamespace:
    message = _msg(text, user_id)
    message.chat = SimpleNamespace(id=-100, type="group")
    return message


@pytest.mark.parametrize("text", ["question", "/new"])
@pytest.mark.parametrize("with_limiter", [True, False])
def test_busy_chat_gets_one_busy_reply_no_llm_call_history_untouched(
    text: str, with_limiter: bool
) -> None:
    llm = ScriptedLLM([ChatResponse(content="first"), ChatResponse(content="second")])
    conv = _conv(llm)
    limiter = RateLimiter(max_per_window=100, window_s=60.0) if with_limiter else None
    assert _send(_group_msg("hello", 1), conv, limiter) == ["first"]
    history = list(conv._chats[-100])

    conv._in_flight.add(-100)  # simulate user 1's next run still in flight
    path = "new" if text == "/new" else "text"
    before = _chat_busy(path)
    limited_before = {r: _limited(r) for r in ("in_flight", "window")}
    try:
        assert _send(_group_msg(text, 2), conv, limiter) == [_CHAT_BUSY_REPLY]
    finally:
        conv._in_flight.discard(-100)
    assert _chat_busy(path) == before + 1
    # A chat-busy rejection is not a rate-limit rejection.
    assert {r: _limited(r) for r in ("in_flight", "window")} == limited_before
    assert _limited("chat_in_flight") == 0.0
    assert len(llm.chat_calls) == 1
    assert conv._chats[-100] == history
    if limiter is not None:
        assert limiter.try_acquire("2").ok  # user 2's slot was released
        limiter.release("2")

    assert _send(_group_msg("again", 2), conv, limiter) == ["second"]


def test_chat_busy_reply_differs_from_per_user_busy_reply() -> None:
    assert _CHAT_BUSY_REPLY != _BUSY_REPLY
    assert "this chat" in _CHAT_BUSY_REPLY


# --- upload into a busy chat: the chat is reserved before any ingestion --------------


def test_upload_into_busy_chat_is_not_downloaded_or_ingested(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    llm = FakeLLM(reply="ok")
    conv = _conv(llm)
    fake = FakeBot(b"hello world " * 20)
    handler = build_document_handler(fake, kb, conv)
    message = _make_doc_message("notes.txt")
    limiter = RateLimiter(max_per_window=100, window_s=60.0)
    before = _chat_busy("upload")
    try:
        with conv.reserve(message.chat.id):  # another member's run in flight
            replies = _send(message, conv, limiter, None, handler)
        assert replies == [_DOCUMENT_BUSY_REPLY]
        assert "NOT" in _DOCUMENT_BUSY_REPLY
        assert fake.get_file_calls == []
        assert fake.download_calls == []
        assert kb.store.list_documents("1") == []
        assert llm.chat_calls == []
        assert _chat_busy("upload") == before + 1
        assert message.chat.id not in conv._chats
        assert conv._in_flight == set()
        assert limiter.try_acquire("1").ok  # the per-user slot was released
        limiter.release("1")

        # Once the chat is free, the same upload goes through normally.
        replies = _send(message, conv, limiter, None, handler)
        assert replies[-2] == _DOCUMENT_READY_REPLY
        assert replies[-1] == "ok"
        assert len(kb.store.list_documents("1")) == 1
    finally:
        kb.close()


def test_normal_upload_full_sequence_and_notice_in_history(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    llm = FakeLLM(reply="ack")
    conv = _conv(llm)
    handler = build_document_handler(FakeBot(b"hello world " * 20), kb, conv)
    message = _make_doc_message("notes.txt")
    try:
        replies = _send(message, conv, None, None, handler)
        assert replies[:3] == [
            _DOCUMENT_RECEIVED_REPLY,
            _EXTRACTING_REPLY,
            _EMBEDDING_REPLY,
        ]
        assert replies[3].startswith("✅ Ingested ")
        assert replies[4:] == [_DOCUMENT_READY_REPLY, "ack"]
        history = conv._chats[message.chat.id]
        assert history[0].role == "user"
        assert history[0].content.startswith('[document uploaded: "notes.txt"')
        assert conv._in_flight == set()
    finally:
        kb.close()


class _BlockingBot(FakeBot):
    """FakeBot whose download blocks until released (ingestion in progress)."""

    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.entered = threading.Event()
        self.release = threading.Event()

    def download_file(self, file_path: str) -> bytes:
        self.entered.set()
        assert self.release.wait(5), "download never released"
        return super().download_file(file_path)


def test_reservation_held_across_ingest_blocks_same_chat_only(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    llm = FakeLLM(reply="ok")
    conv = _conv(llm)
    fake = _BlockingBot(b"hello world " * 20)
    handler = build_document_handler(fake, kb, conv)
    upload = _make_doc_message("notes.txt")
    upload_replies: list[str] = []
    thread = threading.Thread(
        target=lambda: upload_replies.extend(_send(upload, conv, None, None, handler)),
        daemon=True,
    )
    before = _chat_busy("text")
    thread.start()
    try:
        assert fake.entered.wait(5), "upload never reached the download"
        same_chat = _msg("question", user_id=2)
        same_chat.chat = SimpleNamespace(id=upload.chat.id, type="group")
        assert _send(same_chat, conv, None) == [_CHAT_BUSY_REPLY]
        assert _chat_busy("text") == before + 1
        other_chat = _msg("question", user_id=3)
        assert _send(other_chat, conv, None) == ["ok"]
    finally:
        fake.release.set()
        thread.join(5)
        kb.close()
    assert not thread.is_alive()
    assert upload_replies[-2:] == [_DOCUMENT_READY_REPLY, "ok"]
    assert conv._in_flight == set()
    # The busy text message never entered the upload chat's history.
    contents = [m.content for m in conv._chats[upload.chat.id]]
    assert "question" not in contents


def _api_error(method: str) -> ApiTelegramException:
    return ApiTelegramException(
        method, 400, {"error_code": 400, "description": "Bad Request"}
    )


@pytest.mark.parametrize(
    "case",
    ["get_file", "download", "extract", "embed", "store", "llm_error", "agent_error"],
)
def test_reservation_released_after_upload_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    embedder = FakeEmbedder(EmbeddingError("down")) if case == "embed" else None
    kb = _make_kb(tmp_path, embedder=embedder)
    fake = FakeBot(
        b"hello world " * 20,
        error=_api_error("getFile") if case == "get_file" else None,
        download_error=_api_error("downloadFile") if case == "download" else None,
    )
    if case == "store":

        def locked(doc):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(kb.store, "add_document", locked)
    llm: FakeLLM | ScriptedLLM
    if case == "llm_error":
        llm = FakeLLM(error=LLMError("down"))
    elif case == "agent_error":
        llm = ScriptedLLM([])  # exhausted script → unexpected error
    else:
        llm = FakeLLM(reply="ok")
    conv = _conv(llm)
    handler = build_document_handler(fake, kb, conv)
    file_name = "notes.xyz" if case == "extract" else "notes.txt"
    message = _make_doc_message(file_name)
    try:
        replies = _send(message, conv, None, None, handler)
        assert replies[-1] in {
            "Sorry, I couldn't process that document.",
            "Sorry, I couldn't generate a response.",
        }
        assert _DOCUMENT_BUSY_REPLY not in replies
        assert conv._in_flight == set()
        # The chat is free again: it can be reserved without ChatBusyError.
        with conv.reserve(message.chat.id):
            pass
    finally:
        kb.close()


# --- window ----------------------------------------------------------------------


def test_window_exhausted_user_gets_one_rate_limited_reply() -> None:
    llm = FakeLLM(reply="ok")
    conv = _conv(llm)
    limiter = RateLimiter(max_per_window=2, window_s=60.0)
    assert _send(_msg("a"), conv, limiter) == ["ok"]
    assert _send(_msg("b"), conv, limiter) == ["ok"]
    calls = len(llm.chat_calls)
    before = _limited("window")
    assert _send(_msg("c"), conv, limiter) == [_RATE_LIMITED_REPLY]
    assert _limited("window") == before + 1
    assert len(llm.chat_calls) == calls


def test_new_command_counts_toward_the_window() -> None:
    conv = _conv(FakeLLM(reply="ok"))
    limiter = RateLimiter(max_per_window=1, window_s=60.0)
    _send(_msg("/new"), conv, limiter)
    assert _send(_msg("hi"), conv, limiter) == [_RATE_LIMITED_REPLY]


def test_users_are_independent() -> None:
    conv = _conv(FakeLLM(reply="ok"))
    limiter = RateLimiter(max_per_window=1, window_s=60.0)
    assert _send(_msg("a", user_id=1), conv, limiter) == ["ok"]
    assert _send(_msg("b", user_id=1), conv, limiter) == [_RATE_LIMITED_REPLY]
    assert _send(_msg("c", user_id=2), conv, limiter) == ["ok"]


# --- uploads ---------------------------------------------------------------------


def test_upload_blocked_before_download_when_window_exhausted(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    conv = _conv(FakeLLM(reply="ok"))
    fake = FakeBot(b"hello world")
    handler = build_document_handler(fake, kb, conv)
    limiter = RateLimiter(max_per_window=1, window_s=60.0)
    assert _send(_msg("a"), conv, limiter) == ["ok"]
    replies = _send(_make_doc_message("notes.txt"), conv, limiter, None, handler)
    assert replies == [_RATE_LIMITED_REPLY]
    assert fake.get_file_calls == []
    kb.close()


def test_upload_counts_toward_the_window(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    conv = _conv(FakeLLM(reply="ok"))
    fake = FakeBot(b"hello world " * 20)
    handler = build_document_handler(fake, kb, conv)
    limiter = RateLimiter(max_per_window=1, window_s=60.0)
    replies = _send(_make_doc_message("notes.txt"), conv, limiter, None, handler)
    assert fake.get_file_calls
    assert replies[-1] == "ok"
    assert _send(_msg("next"), conv, limiter) == [_RATE_LIMITED_REPLY]
    kb.close()


# --- release ---------------------------------------------------------------------


def test_limiter_released_after_llm_error() -> None:
    conv = _conv(FakeLLM(error=LLMError("boom")))
    limiter = RateLimiter(max_per_window=100, window_s=60.0)
    _send(_msg("a"), conv, limiter)
    assert limiter.try_acquire("1").ok


def test_limiter_released_after_unexpected_error() -> None:
    class Boom(ConversationManager):
        def handle(self, chat_id: int, text: str) -> str:  # type: ignore[override]
            raise RuntimeError("boom")

    conv = Boom(Agent(FakeLLM(reply="x"), ToolRegistry()))
    limiter = RateLimiter(max_per_window=100, window_s=60.0)
    with pytest.raises(RuntimeError):
        _send(_msg("a"), conv, limiter)
    assert limiter.try_acquire("1").ok


def test_overridden_handle_is_invoked_for_the_upload_turn(tmp_path: Path) -> None:
    """The upload turn goes through ``ConversationManager.handle`` (with the held
    reservation), so a subclass overriding ``handle`` sees it like a text turn."""
    seen: list[tuple[int | str, str, bool]] = []

    class Spy(ConversationManager):
        def handle(self, chat_id, text, *, reservation=None):  # type: ignore[no-untyped-def]
            seen.append((chat_id, text, reservation is not None))
            return super().handle(chat_id, text, reservation=reservation)

    kb = _make_kb(tmp_path)
    conv = Spy(Agent(FakeLLM(reply="ack"), ToolRegistry()))
    handler = build_document_handler(FakeBot(b"hello world " * 20), kb, conv)
    message = _make_doc_message("notes.txt")
    replies = _send(message, conv, None, None, handler)

    assert replies[-1] == "ack"
    assert len(seen) == 1
    chat_id, text, reserved = seen[0]
    assert chat_id == message.chat.id
    assert text.startswith('[document uploaded: "notes.txt"')
    assert reserved
    assert conv._in_flight == set()

    seen.clear()
    _send(_msg("hi"), conv, None)
    assert seen == [(43, "hi", False)]


def test_unauthorized_user_never_touches_the_limiter() -> None:
    class Spy(RateLimiter):
        calls = 0

        def try_acquire(self, user_id: str):  # type: ignore[override]
            Spy.calls += 1
            return super().try_acquire(user_id)

    limiter = Spy(max_per_window=1, window_s=60.0)
    replies = _send(
        _msg("a"), _conv(FakeLLM(reply="ok")), limiter, frozenset({"other"})
    )
    assert replies == ["Sorry, you are not allowed to use this bot."]
    assert Spy.calls == 0


def test_no_limiter_changes_nothing() -> None:
    conv = _conv(FakeLLM(reply="ok"))
    for _ in range(20):
        assert _send(_msg("a"), conv, None) == ["ok"]


def test_rejection_is_logged_with_user_and_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter = RateLimiter(max_per_window=100, window_s=60.0)
    limiter.try_acquire("1")
    with caplog.at_level(logging.INFO, logger="vektor.bot"):
        _send(_msg("SECRET-TEXT"), _conv(FakeLLM(reply="ok")), limiter)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "rate limited user=1 reason=in_flight" in text
    assert "SECRET-TEXT" not in text


# --- composition root --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 4),
        ("8", 8),
        ("1", 1),
        ("64", 64),
        ("65", 64),
        ("10000", 64),
        ("0", 4),
        ("-2", 4),
        ("x", 4),
    ],
)
def test_worker_threads_from_env(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int
) -> None:
    if value is None:
        monkeypatch.delenv("BOT_WORKER_THREADS", raising=False)
    else:
        monkeypatch.setenv("BOT_WORKER_THREADS", value)
    assert _worker_threads_from_env() == expected


def test_build_rate_limiter_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RATE_LIMIT_MAX_MESSAGES", raising=False)
    monkeypatch.delenv("RATE_LIMIT_WINDOW_S", raising=False)
    lim = build_rate_limiter()
    assert (lim._max, lim._window_s) == (10, 60.0)


def test_build_rate_limiter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_MAX_MESSAGES", "3")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_S", "12.5")
    lim = build_rate_limiter()
    assert (lim._max, lim._window_s) == (3, 12.5)


@pytest.mark.parametrize(
    ("count", "window"),
    [("x", "y"), ("0", "0"), ("-1", "-3"), ("1.5", "nan"), ("2.0", "inf")],
)
def test_build_rate_limiter_bad_values_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    count: str,
    window: str,
) -> None:
    monkeypatch.setenv("RATE_LIMIT_MAX_MESSAGES", count)
    monkeypatch.setenv("RATE_LIMIT_WINDOW_S", window)
    with caplog.at_level(logging.WARNING, logger="vektor.bot"):
        lim = build_rate_limiter()
    assert (lim._max, lim._window_s) == (10, 60.0)
    assert len(caplog.records) >= 2


def test_create_bot_accepts_rate_limiter_and_uses_worker_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_WORKER_THREADS", "6")
    seen: dict[str, object] = {}
    real = bot_module.telebot.TeleBot

    def spy(token: str, **kwargs: object):
        seen.update(kwargs)
        return real(token, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bot_module.telebot, "TeleBot", spy)
    bot = create_bot(_conv(FakeLLM(reply="ok")), rate_limiter=RateLimiter())
    assert bot is not None
    assert seen["num_threads"] == 6


def test_worker_threads_clamp_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BOT_WORKER_THREADS", "500")
    with caplog.at_level(logging.WARNING, logger="vektor.bot"):
        assert _worker_threads_from_env() == 64
    assert any("BOT_WORKER_THREADS" in r.getMessage() for r in caplog.records)


def test_validation_rejected_message_still_uses_a_slot() -> None:
    conv = _conv(FakeLLM(reply="ok"))
    limiter = RateLimiter(max_per_window=1, window_s=60.0)
    assert _send(_msg("   "), conv, limiter) == [
        "Your message is empty. Please send some text."
    ]
    assert _send(_msg("hi"), conv, limiter) == [_RATE_LIMITED_REPLY]

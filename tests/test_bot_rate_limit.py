"""Handler-level tests for per-user rate limiting and worker-thread config.

Offline: FakeLLM/ScriptedLLM, a fake TeleBot for uploads, a tmp SQLite kb.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY

import bot as bot_module
from agent.agent import Agent
from agent.conversation import ConversationManager
from agent.rate_limit import RateLimiter
from bot import (
    _BUSY_REPLY,
    _RATE_LIMITED_REPLY,
    _worker_threads_from_env,
    build_document_handler,
    build_rate_limiter,
    create_bot,
    handle_message,
)
from llm import LLMError
from llm.base import ChatResponse
from tests.fakes import FakeLLM, ScriptedLLM
from tests.test_bot_documents import FakeBot, _make_doc_message, _make_kb
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

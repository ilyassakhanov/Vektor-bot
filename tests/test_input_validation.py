"""Tests for inbound message validation (``agent.validation.validate_input``).

Pure-function tests plus handler-level wiring tests for ``bot.handle_message``
(fake LLM, no Telegram, no network).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import bot as bot_module
from agent.validation import TELEGRAM_MESSAGE_LIMIT, Verdict, validate_input
from bot import handle_message
from tests.fakes import FakeLLM
from tests.test_bot import _make_conv, _make_message

# U+200B–U+200D, U+2060, U+FEFF, U+202A–U+202E, U+2066–U+2069 (14 code points).
_STRIP_CODE_POINTS = [
    *range(0x200B, 0x200D + 1),
    0x2060,
    0xFEFF,
    *range(0x202A, 0x202E + 1),
    *range(0x2066, 0x2069 + 1),
]


def test_limit_is_telegram_constant() -> None:
    assert TELEGRAM_MESSAGE_LIMIT == 4096


def test_strip_set_has_fourteen_code_points() -> None:
    assert len(_STRIP_CODE_POINTS) == 14


@pytest.mark.parametrize(
    "text",
    ["", "   \n\t", "\u200b\u200c\u200d\u2060\ufeff", " \u200b \u202e\n"],
    ids=["empty", "whitespace", "zero-width-only", "invisible-and-blank"],
)
def test_blank_input_is_rejected_as_empty(text: str) -> None:
    assert validate_input(text) == Verdict(ok=False, text="", reason="empty")


def test_boundary_4096_chars_is_accepted_unchanged() -> None:
    text = "a" * 4096
    assert validate_input(text) == Verdict(ok=True, text=text, reason="")


def test_4097_chars_is_rejected_as_too_long() -> None:
    assert validate_input("a" * 4097) == Verdict(ok=False, text="", reason="too_long")


def test_length_is_checked_on_raw_input_before_stripping() -> None:
    # 4097 raw chars that would shrink under 4096 after stripping are still too long.
    text = "a" * 4000 + "\u200b" * 97
    assert validate_input(text).reason == "too_long"


@pytest.mark.parametrize("code_point", _STRIP_CODE_POINTS, ids=lambda cp: f"U+{cp:04X}")
def test_each_invisible_code_point_is_stripped(code_point: int) -> None:
    verdict = validate_input(f"he{chr(code_point)}llo")
    assert verdict == Verdict(ok=True, text="hello", reason="")


def test_bidi_override_sample_is_stripped() -> None:
    assert validate_input("\u202eevil\u202c").text == "evil"


@pytest.mark.parametrize(
    "text",
    [
        "Привет, мир",
        "🔥🚀 ok",
        "e\u0301",
        "line1\nline2\tx",
        "  padded  ",
    ],
    ids=[
        "cyrillic",
        "non-zwj-emoji",
        "combining-mark",
        "newline-tab",
        "surrounding-space",
    ],
)
def test_visible_unicode_passes_through_unchanged(text: str) -> None:
    assert validate_input(text) == Verdict(ok=True, text=text, reason="")


@pytest.mark.parametrize(
    "text",
    ["_*[]()~`>#+-=|{}.!", "*bold _italic [link("],
    ids=["markdownv2-metachars", "broken-markup"],
)
def test_markdown_metacharacters_pass_through_unchanged(text: str) -> None:
    assert validate_input(text) == Verdict(ok=True, text=text, reason="")


def test_verdict_is_frozen() -> None:
    verdict = validate_input("hi")
    with pytest.raises(AttributeError):
        verdict.ok = False  # type: ignore[misc]


# --- Handler-level wiring (bot.handle_message text path) --------------------


def _handle(
    message: Any,
    conv: Any,
    *,
    allowed_usernames: frozenset[str] | None = None,
    document_handler: Any = None,
) -> list[str]:
    replies: list[str] = []
    handle_message(
        message,
        conv,
        lambda _msg, text: replies.append(text),
        allowed_usernames,
        document_handler,
    )
    return replies


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", bot_module._EMPTY_MESSAGE_REPLY),
        ("   \n", bot_module._EMPTY_MESSAGE_REPLY),
        ("\u200b\u200d\ufeff", bot_module._EMPTY_MESSAGE_REPLY),
        ("a" * 4097, bot_module._TOO_LONG_MESSAGE_REPLY),
    ],
    ids=["empty", "whitespace", "zero-width-only", "too-long"],
)
def test_handler_rejection_sends_one_reply_and_skips_llm(
    text: str, expected: str
) -> None:
    llm = FakeLLM(reply="should not be sent")
    conv = _make_conv(llm)
    assert _handle(_make_message(text), conv) == [expected]
    assert llm.chat_calls == []


def test_handler_none_text_is_rejected_as_empty() -> None:
    llm = FakeLLM()
    msg = _make_message("")
    msg.text = None
    assert _handle(msg, _make_conv(llm)) == [bot_module._EMPTY_MESSAGE_REPLY]
    assert llm.chat_calls == []


def test_handler_rejection_leaves_history_untouched() -> None:
    llm = FakeLLM(reply="ok")
    conv = _make_conv(llm)
    _handle(_make_message("hello"), conv)
    before = list(conv._chats[42])
    _handle(_make_message("\u200b"), conv)
    _handle(_make_message("x" * 5000), conv)
    assert conv._chats[42] == before
    assert len(llm.chat_calls) == 1


def test_handler_scrubs_zero_width_before_llm() -> None:
    llm = FakeLLM(reply="ok")
    conv = _make_conv(llm)
    replies = _handle(_make_message("he\u200bl\u202elo\ufeff"), conv)
    assert replies == ["ok"]
    messages = llm.chat_calls[0][0]
    user_texts = [m.content for m in messages if m.role == "user"]
    assert user_texts == ["hello"]


def test_handler_document_without_text_still_routed() -> None:
    llm = FakeLLM()
    calls: list[Any] = []
    msg = _make_message("")
    msg.text = None
    msg.document = SimpleNamespace(file_name="a.txt", file_id="f1")
    replies = _handle(
        msg,
        _make_conv(llm),
        document_handler=lambda m, _reply: calls.append(m),
    )
    assert calls == [msg]
    assert replies == []
    assert llm.chat_calls == []


def test_handler_new_command_still_works() -> None:
    llm = FakeLLM(reply="ok")
    conv = _make_conv(llm)
    _handle(_make_message("hello"), conv)
    replies = _handle(_make_message("/new"), conv)
    assert len(replies) == 1
    assert replies[0] not in (
        bot_module._EMPTY_MESSAGE_REPLY,
        bot_module._TOO_LONG_MESSAGE_REPLY,
    )
    assert 42 not in conv._chats
    assert len(llm.chat_calls) == 1


def test_handler_unauthorized_empty_message_gets_auth_denied() -> None:
    llm = FakeLLM()
    msg = _make_message("")
    msg.from_user.username = "intruder"
    replies = _handle(msg, _make_conv(llm), allowed_usernames=frozenset({"someone"}))
    assert replies == [bot_module._AUTH_DENIED_REPLY]
    assert llm.chat_calls == []

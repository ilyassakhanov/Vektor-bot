"""Tests for outbound reply splitting (``agent.validation.split_reply``).

Pure-function tests plus handler wiring (fake reply recorder, ScriptedLLM,
tmp SQLite kb): no Telegram, no Ollama, no network. Lengths are measured
in UTF-16 code units, Telegram's message-length unit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent.validation import TELEGRAM_MESSAGE_LIMIT, split_reply
from bot import (
    _DOCUMENT_READY_REPLY,
    _LLM_ERROR_REPLY,
    build_document_handler,
    handle_message,
)
from llm.base import ChatResponse
from tests.fakes import ScriptedLLM
from tests.test_bot import _make_conv, _make_message
from tests.test_bot_documents import (
    FakeBot,
    _make_doc_message,
    _make_kb,
    _staged_progress,
)


def _units(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _assert_invariants(text: str, chunks: list[str], limit: int) -> None:
    assert "".join(chunks) == text
    assert all(_units(c) <= limit for c in chunks)
    assert all(c for c in chunks)
    # No chunk starts or ends inside a surrogate pair (str slicing guarantees
    # this; encoding with strict errors proves no lone surrogates exist).
    for c in chunks:
        c.encode("utf-16-le")


def _prose(total: int) -> str:
    sentence = "The quick brown fox jumps over the lazy dog. "
    paragraph = sentence * 8 + "\n\n"
    out = ""
    while len(out) < total:
        out += paragraph
    return out[:total]


@pytest.mark.parametrize("text", ["", "short reply", "x" * TELEGRAM_MESSAGE_LIMIT])
def test_text_that_fits_is_returned_as_is(text: str) -> None:
    assert split_reply(text) == [text]


def test_boundary_4096_ascii_is_one_chunk() -> None:
    assert len(split_reply("a" * 4096)) == 1


def test_10k_prose_splits_into_ordered_chunks_on_paragraphs() -> None:
    text = _prose(10_000)
    chunks = split_reply(text)
    assert len(chunks) > 1
    _assert_invariants(text, chunks, TELEGRAM_MESSAGE_LIMIT)
    for chunk in chunks[:-1]:
        assert chunk.endswith("\n\n")


def test_sentence_boundary_used_when_no_paragraph_break() -> None:
    text = "The quick brown fox jumps over the lazy dog. " * 250  # ~11k chars
    chunks = split_reply(text)
    assert len(chunks) > 1
    _assert_invariants(text, chunks, TELEGRAM_MESSAGE_LIMIT)
    for chunk in chunks[:-1]:
        assert chunk.endswith(". ")


def test_word_boundary_used_when_no_sentence_end() -> None:
    text = "word " * 2000  # 10k chars, no sentence punctuation
    chunks = split_reply(text)
    assert len(chunks) > 1
    _assert_invariants(text, chunks, TELEGRAM_MESSAGE_LIMIT)
    for chunk in chunks[:-1]:
        assert chunk.endswith(" ")


def test_astral_emoji_split_by_utf16_units() -> None:
    text = "😀" * 3000  # 3000 chars, 6000 UTF-16 units
    chunks = split_reply(text)
    assert len(chunks) >= 2
    _assert_invariants(text, chunks, TELEGRAM_MESSAGE_LIMIT)


def test_odd_limit_never_splits_surrogate_pair() -> None:
    text = "😀" * 10
    chunks = split_reply(text, limit=3)
    _assert_invariants(text, chunks, 3)
    assert all(c == "😀" for c in chunks)


def test_unbreakable_token_is_hard_cut() -> None:
    text = "x" * 5000
    chunks = split_reply(text)
    assert chunks == ["x" * 4096, "x" * 904]


def test_small_limit_boundary_priority() -> None:
    text = "ab cd.\n\nef gh. ij kl"
    chunks = split_reply(text, limit=10)
    _assert_invariants(text, chunks, 10)
    assert chunks[0] == "ab cd.\n\n"
    assert chunks[1] == "ef gh. "
    assert chunks[2] == "ij kl"


def test_newline_counts_as_sentence_boundary() -> None:
    text = "aaaa bbbb\ncccc dddd"
    chunks = split_reply(text, limit=12)
    _assert_invariants(text, chunks, 12)
    assert chunks[0] == "aaaa bbbb\n"


# --- Handler wiring: both agent-reply send sites ---------------------------------

_META_REPLY = (
    "_*[]()~`>#+-=|{}.! *bold _italic [link](http://x ~strike `code "
    "\\escaped >quote #tag +plus -minus =eq |pipe {brace} dot. bang!"
)


class _Recorder:
    """Records every send as ``(args, kwargs)`` — catches stray kwargs."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))

    @property
    def texts(self) -> list[str]:
        return [args[1] for args, _kwargs in self.calls]


def _send_text(reply: str) -> _Recorder:
    rec = _Recorder()
    llm = ScriptedLLM([ChatResponse(content=reply)])
    handle_message(_make_message("hello"), _make_conv(llm), rec)
    return rec


def _assert_no_parse_mode(rec: _Recorder) -> None:
    assert all("parse_mode" not in kwargs for _args, kwargs in rec.calls)


def test_10k_reply_sent_as_ordered_chunks_within_limit() -> None:
    reply = _prose(10_000)
    rec = _send_text(reply)
    assert len(rec.calls) > 1
    assert all(_units(t) <= TELEGRAM_MESSAGE_LIMIT for t in rec.texts)
    assert "".join(rec.texts) == reply
    _assert_no_parse_mode(rec)


def test_exactly_4096_char_reply_is_one_send() -> None:
    reply = "a" * TELEGRAM_MESSAGE_LIMIT
    rec = _send_text(reply)
    assert rec.texts == [reply]


def test_emoji_reply_respects_utf16_limit() -> None:
    reply = "😀" * 3000
    rec = _send_text(reply)
    assert len(rec.calls) >= 2
    assert all(_units(t) <= TELEGRAM_MESSAGE_LIMIT for t in rec.texts)
    assert "".join(rec.texts) == reply


def test_markdown_metacharacters_delivered_unchanged_without_parse_mode() -> None:
    rec = _send_text(_META_REPLY)
    assert rec.texts == [_META_REPLY]
    _assert_no_parse_mode(rec)


def test_whitespace_only_trailing_chunk_is_skipped() -> None:
    reply = "a" * TELEGRAM_MESSAGE_LIMIT + "\n\n"
    assert split_reply(reply)[-1].strip() == ""  # premise: blank last chunk
    rec = _send_text(reply)
    assert rec.texts == ["a" * TELEGRAM_MESSAGE_LIMIT]
    assert all(t.strip() for t in rec.texts)


def test_document_agent_turn_is_split_after_staged_progress(tmp_path: Path) -> None:
    reply = _prose(10_000)
    llm = ScriptedLLM([ChatResponse(content=reply)])
    kb = _make_kb(tmp_path)
    handler = build_document_handler(
        FakeBot(" ".join(["needle"] * 20).encode()), kb, _make_conv(llm)
    )
    rec = _Recorder()
    handler(_make_doc_message("notes.txt"), rec)
    texts = rec.texts
    assert texts[:3] == _staged_progress()
    assert texts[3].startswith("✅ Ingested ")
    assert texts[4] == _DOCUMENT_READY_REPLY
    agent_chunks = texts[5:]
    assert len(agent_chunks) > 1
    assert all(_units(t) <= TELEGRAM_MESSAGE_LIMIT for t in agent_chunks)
    assert "".join(agent_chunks) == reply
    _assert_no_parse_mode(rec)


def test_whitespace_only_reply_sends_one_fallback() -> None:
    rec = _send_text("   \n")
    assert rec.texts == [_LLM_ERROR_REPLY]
    _assert_no_parse_mode(rec)


def test_document_agent_turn_whitespace_reply_sends_one_fallback(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM([ChatResponse(content="   \n")])
    kb = _make_kb(tmp_path)
    handler = build_document_handler(
        FakeBot(" ".join(["needle"] * 20).encode()), kb, _make_conv(llm)
    )
    rec = _Recorder()
    handler(_make_doc_message("notes.txt"), rec)
    assert rec.texts[4] == _DOCUMENT_READY_REPLY
    assert rec.texts[5:] == [_LLM_ERROR_REPLY]

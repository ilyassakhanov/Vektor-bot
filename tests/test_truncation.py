"""Tests for the shared tool-output truncation helper."""

from __future__ import annotations

import logging
import re

from tools.truncation import (
    _MIN_MAX_CHARS,
    max_output_chars_from_env,
    truncate,
)


def test_truncate_short_text_unchanged():
    text = "short text well under the cap"
    assert truncate(text, 100) == text


def test_truncate_long_text_head_tail_marker():
    text = "H" * 40 + "m" * 200 + "T" * 40
    result = truncate(text, 60)
    match = re.search(r"\[truncated (\d+) chars\]", result)
    assert match is not None
    head_len = len(result) - len(result.lstrip("H"))
    tail_len = len(result) - len(result.rstrip("T"))
    dropped = int(match.group(1))
    assert dropped == len(text) - head_len - tail_len
    assert dropped > 0
    assert len(result) <= 60


def test_truncate_preserves_head_and_tail():
    text = "HEADMARKER" + "m" * 300 + "TAILMARKER"
    result = truncate(text, 80)
    assert result.startswith("HEADMARKER")
    assert result.endswith("TAILMARKER")


def test_truncate_exactly_at_cap_unchanged():
    text = "x" * 100
    assert truncate(text, 100) == text


def test_truncate_one_char_under_cap_unchanged():
    text = "x" * 99
    assert truncate(text, 100) == text


def test_truncate_one_char_over_cap_truncates():
    text = "x" * 101
    result = truncate(text, 100)
    assert "[truncated " in result
    assert len(result) <= 100


def test_truncate_empty_string_unchanged():
    assert truncate("", 100) == ""


# --- Env-resolved cap floor -------------------------------------------------


def test_env_cap_below_marker_floor_falls_back(monkeypatch, caplog):
    for raw in ("10", "29"):
        monkeypatch.setenv("EXEC_MAX_OUTPUT_CHARS", raw)
        with caplog.at_level(logging.WARNING):
            assert max_output_chars_from_env() == 4000
        assert "Invalid EXEC_MAX_OUTPUT_CHARS" in caplog.text
        caplog.clear()


def test_env_cap_at_floor_is_accepted(monkeypatch):
    monkeypatch.setenv("EXEC_MAX_OUTPUT_CHARS", str(_MIN_MAX_CHARS))
    assert max_output_chars_from_env() == _MIN_MAX_CHARS


def test_truncate_at_floor_produces_exact_cap_output():
    text = "H" * 30 + "m" * 10_000 + "T" * 30
    result = truncate(text, _MIN_MAX_CHARS)
    match = re.search(r"\[truncated (\d+) chars\]", result)
    assert match is not None
    head_len = len(result) - len(result.lstrip("H"))
    tail_len = len(result) - len(result.rstrip("T"))
    assert head_len >= 1
    assert tail_len >= 1
    assert int(match.group(1)) == len(text) - head_len - tail_len
    assert len(result) == _MIN_MAX_CHARS


def test_truncate_floor_covers_large_dropped_counts():
    for length in (10**3, 10**5, 10**6):
        result = truncate("m" * length, _MIN_MAX_CHARS)
        assert len(result) <= _MIN_MAX_CHARS
        assert "[truncated " in result

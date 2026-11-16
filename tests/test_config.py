"""Tests for ``config.positive_env`` — shared warn-and-fallback env parsing."""

from __future__ import annotations

import logging

import pytest

from config import positive_env

_NAME = "VEKTOR_TEST_POSITIVE_ENV"


def test_unset_returns_default_without_warning(monkeypatch, caplog):
    monkeypatch.delenv(_NAME, raising=False)
    with caplog.at_level(logging.WARNING, logger="vektor"):
        assert positive_env(_NAME, 7, int) == 7
    assert not caplog.records


@pytest.mark.parametrize(
    ("raw", "parse", "expected"), [("3", int, 3), ("2.5", float, 2.5)]
)
def test_valid_value_is_parsed(monkeypatch, raw, parse, expected):
    monkeypatch.setenv(_NAME, raw)
    value = positive_env(_NAME, 9, parse)
    assert value == expected
    assert type(value) is parse


@pytest.mark.parametrize(
    ("raw", "parse"),
    [
        ("0", int),
        ("-1", int),
        ("abc", int),
        ("2.5", int),
        ("", int),
        ("0", float),
        ("nan", float),
        ("inf", float),
        ("-0.5", float),
    ],
)
def test_invalid_value_warns_and_falls_back(monkeypatch, caplog, raw, parse):
    monkeypatch.setenv(_NAME, raw)
    with caplog.at_level(logging.WARNING, logger="vektor"):
        assert positive_env(_NAME, 9, parse) == 9
    assert any(_NAME in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("parse", [int, float])
def test_huge_value_does_not_crash_and_falls_back(monkeypatch, caplog, parse):
    """A huge int overflows ``math.isfinite``; it must warn, not raise."""
    monkeypatch.setenv(_NAME, "9" * 400)
    with caplog.at_level(logging.WARNING, logger="vektor"):
        assert positive_env(_NAME, 9, parse) == 9
    assert any(_NAME in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("raw", "parse", "maximum"), [("1e15", float, 3600.0), ("100", int, 64)]
)
def test_value_above_maximum_is_clamped_with_warning(
    monkeypatch, caplog, raw, parse, maximum
):
    monkeypatch.setenv(_NAME, raw)
    with caplog.at_level(logging.WARNING, logger="vektor"):
        assert positive_env(_NAME, 9, parse, maximum) == maximum
    assert any("above the maximum" in r.getMessage() for r in caplog.records)


def test_value_at_maximum_is_kept_silently(monkeypatch, caplog):
    monkeypatch.setenv(_NAME, "3600")
    with caplog.at_level(logging.WARNING, logger="vektor"):
        assert positive_env(_NAME, 9.0, float, 3600.0) == 3600.0
    assert not caplog.records

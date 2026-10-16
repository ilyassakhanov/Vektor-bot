"""Tests for RetrievalConfig — env parsing, defaults, warn+default fallbacks."""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path

import pytest

from retrieval.config import RetrievalConfig

_LOGGER_NAME = "vektor.retrieval.config"

INT_FIELDS: list[tuple[str, str, int]] = [
    ("KB_CHUNK_SIZE", "kb_chunk_size", 800),
    ("KB_CHUNK_OVERLAP", "kb_chunk_overlap", 100),
    ("KB_VECTOR_LIMIT", "kb_vector_limit", 20),
    ("KB_FTS_LIMIT", "kb_fts_limit", 20),
    ("KB_TOP_K", "kb_top_k", 5),
    ("KB_RRF_K", "kb_rrf_k", 60),
]

BOOL_FIELDS: list[tuple[str, str]] = [
    ("KB_ENABLED", "kb_enabled"),
    ("KB_FTS_ENABLED", "kb_fts_enabled"),
    ("KB_EXPANSION_ENABLED", "kb_expansion_enabled"),
    ("KB_RERANK_ENABLED", "kb_rerank_enabled"),
]


@pytest.fixture(autouse=True)
def clean_retrieval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every retrieval-related env var so each test starts from defaults."""
    for name in list(os.environ):
        if name.startswith("KB_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("OLLAMA_EMBED_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_EXPANSION_MODEL", raising=False)


def test_defaults_when_all_unset() -> None:
    cfg = RetrievalConfig.from_env()
    assert cfg == RetrievalConfig()
    assert cfg.kb_enabled is True
    assert cfg.kb_db_path == Path("data/vektor.db")
    assert cfg.kb_chunk_size == 800
    assert cfg.kb_chunk_overlap == 100
    assert cfg.kb_vector_limit == 20
    assert cfg.kb_fts_limit == 20
    assert cfg.kb_top_k == 5
    assert cfg.kb_rrf_k == 60
    assert cfg.kb_fts_enabled is True
    assert cfg.kb_expansion_enabled is True
    assert cfg.ollama_expansion_model == "qwen3:0.6b"
    assert cfg.kb_expansion_timeout == 10.0
    assert cfg.kb_expansion_temperature == 0.0
    assert cfg.kb_rerank_enabled is True
    assert cfg.kb_rerank_timeout == 10.0
    assert cfg.ollama_embed_model == "qwen3-embedding:0.6b"


def test_unset_vars_are_silent(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    RetrievalConfig.from_env()
    assert caplog.text == ""


@pytest.mark.parametrize(("var", "field"), BOOL_FIELDS)
@pytest.mark.parametrize("token", ["1", "true", "YES", "On"])
def test_bool_true_tokens(
    monkeypatch: pytest.MonkeyPatch, var: str, field: str, token: str
) -> None:
    monkeypatch.setenv(var, token)
    assert getattr(RetrievalConfig.from_env(), field) is True


@pytest.mark.parametrize(("var", "field"), BOOL_FIELDS)
@pytest.mark.parametrize("token", ["0", "FALSE", "no", "OFF"])
def test_bool_false_tokens(
    monkeypatch: pytest.MonkeyPatch, var: str, field: str, token: str
) -> None:
    monkeypatch.setenv(var, token)
    assert getattr(RetrievalConfig.from_env(), field) is False


@pytest.mark.parametrize(("var", "field"), BOOL_FIELDS)
def test_unknown_bool_token_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    var: str,
    field: str,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    monkeypatch.setenv(var, "maybe")
    cfg = RetrievalConfig.from_env()
    assert getattr(cfg, field) is True
    assert var in caplog.text
    assert "maybe" in caplog.text


def test_valid_int_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for var, value in {
        "KB_CHUNK_SIZE": "123",
        "KB_CHUNK_OVERLAP": "45",
        "KB_VECTOR_LIMIT": "7",
        "KB_FTS_LIMIT": "9",
        "KB_TOP_K": "3",
        "KB_RRF_K": "11",
    }.items():
        monkeypatch.setenv(var, value)
    cfg = RetrievalConfig.from_env()
    assert cfg.kb_chunk_size == 123
    assert cfg.kb_chunk_overlap == 45
    assert cfg.kb_vector_limit == 7
    assert cfg.kb_fts_limit == 9
    assert cfg.kb_top_k == 3
    assert cfg.kb_rrf_k == 11


def test_zero_chunk_overlap_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_CHUNK_OVERLAP", "0")
    assert RetrievalConfig.from_env().kb_chunk_overlap == 0


def test_chunk_pair_overlap_ge_size_falls_back_to_both_defaults(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """KB_CHUNK_SIZE=50 is valid alone but clashes with default overlap 100."""
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    monkeypatch.setenv("KB_CHUNK_SIZE", "50")
    cfg = RetrievalConfig.from_env()
    assert cfg.kb_chunk_size == 800
    assert cfg.kb_chunk_overlap == 100
    assert "KB_CHUNK_SIZE" in caplog.text
    assert "KB_CHUNK_OVERLAP" in caplog.text


def test_chunk_pair_overlap_above_default_size_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    monkeypatch.setenv("KB_CHUNK_OVERLAP", "900")
    cfg = RetrievalConfig.from_env()
    assert cfg.kb_chunk_size == 800
    assert cfg.kb_chunk_overlap == 100
    assert "KB_CHUNK_OVERLAP" in caplog.text


def test_chunk_pair_equal_overlap_and_size_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    monkeypatch.setenv("KB_CHUNK_SIZE", "100")
    monkeypatch.setenv("KB_CHUNK_OVERLAP", "100")
    cfg = RetrievalConfig.from_env()
    assert cfg.kb_chunk_size == 800
    assert cfg.kb_chunk_overlap == 100


def test_chunk_pair_valid_pair_is_kept_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    monkeypatch.setenv("KB_CHUNK_SIZE", "50")
    monkeypatch.setenv("KB_CHUNK_OVERLAP", "10")
    cfg = RetrievalConfig.from_env()
    assert cfg.kb_chunk_size == 50
    assert cfg.kb_chunk_overlap == 10
    assert caplog.text == ""


@pytest.mark.parametrize(("var", "field", "default"), INT_FIELDS)
def test_non_numeric_int_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    var: str,
    field: str,
    default: int,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    monkeypatch.setenv(var, "not-a-number")
    cfg = RetrievalConfig.from_env()
    assert getattr(cfg, field) == default
    assert var in caplog.text
    assert "not-a-number" in caplog.text


@pytest.mark.parametrize(
    ("var", "field", "default", "value"),
    [
        ("KB_CHUNK_SIZE", "kb_chunk_size", 800, "0"),
        ("KB_CHUNK_SIZE", "kb_chunk_size", 800, "-5"),
        ("KB_CHUNK_OVERLAP", "kb_chunk_overlap", 100, "-1"),
        ("KB_VECTOR_LIMIT", "kb_vector_limit", 20, "0"),
        ("KB_FTS_LIMIT", "kb_fts_limit", 20, "0"),
        ("KB_TOP_K", "kb_top_k", 5, "0"),
        ("KB_RRF_K", "kb_rrf_k", 60, "0"),
    ],
)
def test_out_of_range_int_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    var: str,
    field: str,
    default: int,
    value: str,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    monkeypatch.setenv(var, value)
    cfg = RetrievalConfig.from_env()
    assert getattr(cfg, field) == default
    assert var in caplog.text


def test_valid_db_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    expected = tmp_path / "kb.db"
    monkeypatch.setenv("KB_DB_PATH", str(expected))
    assert RetrievalConfig.from_env().kb_db_path == expected


def test_valid_model_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_EXPANSION_MODEL", "llama3.2:1b")
    monkeypatch.setenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    cfg = RetrievalConfig.from_env()
    assert cfg.ollama_expansion_model == "llama3.2:1b"
    assert cfg.ollama_embed_model == "nomic-embed-text"


@pytest.mark.parametrize(
    ("var", "field", "default"),
    [
        ("OLLAMA_EXPANSION_MODEL", "ollama_expansion_model", "qwen3:0.6b"),
        ("OLLAMA_EMBED_MODEL", "ollama_embed_model", "qwen3-embedding:0.6b"),
    ],
)
@pytest.mark.parametrize("value", ["", "   "])
def test_empty_model_name_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    var: str,
    field: str,
    default: str,
    value: str,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    monkeypatch.setenv(var, value)
    cfg = RetrievalConfig.from_env()
    assert getattr(cfg, field) == default
    assert var in caplog.text


def test_valid_float_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_EXPANSION_TIMEOUT", "2.5")
    monkeypatch.setenv("KB_EXPANSION_TEMPERATURE", "0.7")
    monkeypatch.setenv("KB_RERANK_TIMEOUT", "3.5")
    cfg = RetrievalConfig.from_env()
    assert cfg.kb_expansion_timeout == 2.5
    assert cfg.kb_expansion_temperature == 0.7
    assert cfg.kb_rerank_timeout == 3.5


def test_zero_temperature_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_EXPANSION_TEMPERATURE", "0")
    assert RetrievalConfig.from_env().kb_expansion_temperature == 0.0


@pytest.mark.parametrize("value", ["abc", "0", "-3", "nan", "inf", "-inf"])
def test_invalid_timeout_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    monkeypatch.setenv("KB_EXPANSION_TIMEOUT", value)
    cfg = RetrievalConfig.from_env()
    assert cfg.kb_expansion_timeout == 10.0
    assert "KB_EXPANSION_TIMEOUT" in caplog.text
    assert value in caplog.text


@pytest.mark.parametrize("value", ["abc", "0", "-3", "nan", "inf", "-inf"])
def test_invalid_rerank_timeout_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    monkeypatch.setenv("KB_RERANK_TIMEOUT", value)
    cfg = RetrievalConfig.from_env()
    assert cfg.kb_rerank_timeout == 10.0
    assert "KB_RERANK_TIMEOUT" in caplog.text
    assert value in caplog.text


@pytest.mark.parametrize("value", ["abc", "-0.1", "nan", "inf"])
def test_negative_temperature_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    monkeypatch.setenv("KB_EXPANSION_TEMPERATURE", value)
    cfg = RetrievalConfig.from_env()
    assert cfg.kb_expansion_temperature == 0.0
    assert "KB_EXPANSION_TEMPERATURE" in caplog.text


def test_config_is_frozen() -> None:
    cfg = RetrievalConfig.from_env()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.kb_top_k = 1  # type: ignore[misc]

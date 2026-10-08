"""Retrieval configuration — the single place retrieval env vars are read.

``RetrievalConfig`` is a frozen dataclass holding every knob of the retrieval
subsystem. :meth:`RetrievalConfig.from_env` is the only boundary through which
``KB_*`` / ``OLLAMA_EMBED_MODEL`` / ``OLLAMA_EXPANSION_MODEL`` enter the code;
no other module may read them. Invalid values never crash — each logs a
warning naming the variable, the bad raw value, and the default used, then
falls back to the documented default (mirroring ``_metrics_port_from_env`` in
``bot.py``). Unset variables use defaults silently.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("vektor.retrieval.config")

_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class RetrievalConfig:
    """Frozen configuration for the retrieval subsystem.

    Field defaults are the documented configuration defaults; ``from_env``
    reads them from the environment with warn-and-fallback semantics.
    """

    kb_enabled: bool = True
    kb_db_path: Path = Path("data/vektor.db")
    kb_chunk_size: int = 800
    kb_chunk_overlap: int = 100
    kb_vector_limit: int = 20
    kb_fts_limit: int = 20
    kb_top_k: int = 5
    kb_rrf_k: int = 60
    kb_fts_enabled: bool = True
    kb_expansion_enabled: bool = True
    ollama_expansion_model: str = "qwen3:0.6b"
    kb_expansion_timeout: float = 10.0
    kb_expansion_temperature: float = 0.0
    kb_rerank_enabled: bool = True
    kb_rerank_timeout: float = 10.0
    ollama_embed_model: str = "qwen3-embedding:0.6b"

    @classmethod
    def from_env(cls) -> RetrievalConfig:
        """Build a config from the environment — the single read boundary.

        ``KB_CHUNK_SIZE`` and ``KB_CHUNK_OVERLAP`` are validated as a pair:
        each variable alone only needs its individual minimum, but an
        overlap that is not smaller than the size would make the first
        ``chunk_text`` call raise ``ValueError`` — so the invalid pair falls
        back to both documented defaults instead.
        """
        defaults = cls()
        chunk_size = _env_int("KB_CHUNK_SIZE", defaults.kb_chunk_size, 1)
        chunk_overlap = _env_int("KB_CHUNK_OVERLAP", defaults.kb_chunk_overlap, 0)
        if chunk_overlap >= chunk_size:
            log.warning(
                "Invalid KB_CHUNK_SIZE=%d / KB_CHUNK_OVERLAP=%d pair"
                " (need 0 <= overlap < size), using defaults %d/%d",
                chunk_size,
                chunk_overlap,
                defaults.kb_chunk_size,
                defaults.kb_chunk_overlap,
            )
            chunk_size = defaults.kb_chunk_size
            chunk_overlap = defaults.kb_chunk_overlap
        return cls(
            kb_enabled=_env_bool("KB_ENABLED", defaults.kb_enabled),
            kb_db_path=Path(_env_str("KB_DB_PATH", str(defaults.kb_db_path))),
            kb_chunk_size=chunk_size,
            kb_chunk_overlap=chunk_overlap,
            kb_vector_limit=_env_int("KB_VECTOR_LIMIT", defaults.kb_vector_limit, 1),
            kb_fts_limit=_env_int("KB_FTS_LIMIT", defaults.kb_fts_limit, 1),
            kb_top_k=_env_int("KB_TOP_K", defaults.kb_top_k, 1),
            kb_rrf_k=_env_int("KB_RRF_K", defaults.kb_rrf_k, 1),
            kb_fts_enabled=_env_bool("KB_FTS_ENABLED", defaults.kb_fts_enabled),
            kb_expansion_enabled=_env_bool(
                "KB_EXPANSION_ENABLED", defaults.kb_expansion_enabled
            ),
            ollama_expansion_model=_env_str(
                "OLLAMA_EXPANSION_MODEL", defaults.ollama_expansion_model
            ),
            kb_expansion_timeout=_env_float(
                "KB_EXPANSION_TIMEOUT", defaults.kb_expansion_timeout, 0.0, True
            ),
            kb_expansion_temperature=_env_float(
                "KB_EXPANSION_TEMPERATURE",
                defaults.kb_expansion_temperature,
                0.0,
                False,
            ),
            kb_rerank_enabled=_env_bool(
                "KB_RERANK_ENABLED", defaults.kb_rerank_enabled
            ),
            kb_rerank_timeout=_env_float(
                "KB_RERANK_TIMEOUT", defaults.kb_rerank_timeout, 0.0, True
            ),
            ollama_embed_model=_env_str(
                "OLLAMA_EMBED_MODEL", defaults.ollama_embed_model
            ),
        )


def _env_bool(name: str, default: bool) -> bool:
    """Read ``name`` as a boolean token; unknown tokens warn and fall back."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    log.warning("Invalid %s %r, using default %r", name, raw, default)
    return default


def _env_str(name: str, default: str) -> str:
    """Read ``name`` as a non-empty string; empty values warn and fall back."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not raw.strip():
        log.warning("Empty %s, using default %r", name, default)
        return default
    return raw


def _env_int(name: str, default: int, minimum: int) -> int:
    """Read ``name`` as an int; unparseable or below-minimum warns and falls back."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("Invalid %s %r, using default %d", name, raw, default)
        return default
    if value < minimum:
        log.warning("Invalid %s %r, using default %d", name, raw, default)
        return default
    return value


def _env_float(name: str, default: float, minimum: float, exclusive: bool) -> float:
    """Read ``name`` as a float; invalid or out-of-range warns and falls back.

    ``minimum`` is inclusive unless ``exclusive`` is True. Non-finite values
    (NaN, infinity) are always rejected.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("Invalid %s %r, using default %s", name, raw, default)
        return default
    if (
        not math.isfinite(value)
        or (value < minimum)
        or (exclusive and value == minimum)
    ):
        log.warning("Invalid %s %r, using default %s", name, raw, default)
        return default
    return value

"""Notional token-cost model.

Prices are USD per 1M tokens and are **notional**: a local Ollama instance
costs $0 in reality. They exist so before/after benchmark comparisons and
estimated-cost reporting have meaning. Defaults approximate a small hosted
model ($0.35 input / $1.25 output per 1M tokens).

Cached tokens are billed as free input — prompt-cache hits cost nothing
locally and are heavily discounted on hosted providers — so the cost of a
call is ``max(input − cached, 0) · price_in/1M + output · price_out/1M``.
"""

from __future__ import annotations

import logging
import math
import os

from llm.base import TokenUsage

log = logging.getLogger("vektor.llm.cost")

_DEFAULT_PRICE_IN_PER_1M = 0.35
_DEFAULT_PRICE_OUT_PER_1M = 1.25

_ENV_PRICE_IN = "LLM_PRICE_IN_PER_1M"
_ENV_PRICE_OUT = "LLM_PRICE_OUT_PER_1M"

_PRICES_PER_1M: dict[str, tuple[float, float]] = {
    "llama3.2": (_DEFAULT_PRICE_IN_PER_1M, _DEFAULT_PRICE_OUT_PER_1M),
    "qwen3.5:9b": (_DEFAULT_PRICE_IN_PER_1M, _DEFAULT_PRICE_OUT_PER_1M),
}


def _price_from_env(name: str, fallback: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        log.warning("Invalid %s=%r, falling back to table price", name, raw)
        return fallback
    if not math.isfinite(value) or value < 0:
        log.warning("Invalid %s=%r, falling back to table price", name, raw)
        return fallback
    return value


def estimate_cost(usage: TokenUsage) -> float:
    """Return the notional USD cost of a single LLM call.

    The per-model price table falls back to the default pair for unknown
    models. Env overrides ``LLM_PRICE_IN_PER_1M`` / ``LLM_PRICE_OUT_PER_1M``
    take precedence over the table for every model; invalid values fall back
    to the table with a warning.
    """
    price_in, price_out = _PRICES_PER_1M.get(
        usage.model, (_DEFAULT_PRICE_IN_PER_1M, _DEFAULT_PRICE_OUT_PER_1M)
    )
    price_in = _price_from_env(_ENV_PRICE_IN, price_in)
    price_out = _price_from_env(_ENV_PRICE_OUT, price_out)
    billed_input = max(usage.input_tokens - usage.cached_tokens, 0)
    return billed_input * price_in / 1e6 + usage.output_tokens * price_out / 1e6

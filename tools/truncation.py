"""Shared tool-output truncation — head+tail with an explicit marker.

Tool results enter the LLM context every turn, so over-cap output is
bounded: the head and tail of the text are kept (half of the remaining
budget each) and joined by an explicit ``... [truncated N chars] ...``
marker, where N is the number of dropped original characters. The marker
is budgeted inside the cap, so truncated output is exactly ``max_chars``
whenever the cap can fit head, marker, and tail. Caps too small for any
such split — unreachable through ``EXEC_MAX_OUTPUT_CHARS``, which rejects
them — degrade to a marker-only result.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("vektor.tools.truncation")

_DEFAULT_MAX_OUTPUT_CHARS = 4000
_ENV_MAX_OUTPUT_CHARS = "EXEC_MAX_OUTPUT_CHARS"
_MAX_PLAUSIBLE_TOOL_OUTPUT_CHARS = 10**13
_MIN_MAX_CHARS = len(f"... [truncated {_MAX_PLAUSIBLE_TOOL_OUTPUT_CHARS} chars] ...")


def truncate(text: str, max_chars: int) -> str:
    """Truncate ``text`` to at most ``max_chars`` characters, marker included.

    Text at or below the cap is returned unchanged. Otherwise the head and
    tail each get half of the budget left after the marker, and the marker
    reports the exact number of dropped original characters. A cap too
    small to fit any head/marker/tail split yields just the marker.
    """
    if len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    head = 0
    tail = 0
    marker = ""
    while True:
        marker = f"... [truncated {dropped} chars] ..."
        kept = max_chars - len(marker)
        if kept > 0:
            head = kept // 2
            tail = kept - head
        else:
            head = 0
            tail = 0
        actual_dropped = len(text) - head - tail
        if actual_dropped == dropped:
            break
        dropped = actual_dropped
    tail_text = text[len(text) - tail :] if tail > 0 else ""
    return f"{text[:head]}{marker}{tail_text}"


def max_output_chars_from_env(value: int | None = None) -> int:
    """Resolve the tool-output cap: explicit value, else env, else default.

    Reads ``EXEC_MAX_OUTPUT_CHARS`` (default 4000). Environment values that
    are non-numeric, non-positive, or below ``_MIN_MAX_CHARS`` — the
    truncation-marker length for a dropped count of
    ``_MAX_PLAUSIBLE_TOOL_OUTPUT_CHARS`` (≈10 TB, beyond any in-memory tool
    output), i.e. the smallest cap that still fits the marker for any
    possible text — fall back to the default with a warning.
    """
    if value is not None:
        return value
    raw = os.environ.get(_ENV_MAX_OUTPUT_CHARS)
    if raw is None:
        return _DEFAULT_MAX_OUTPUT_CHARS
    parsed: int | None
    try:
        parsed = int(raw)
    except ValueError:
        parsed = None
    if parsed is None or parsed < _MIN_MAX_CHARS:
        log.warning(
            "Invalid %s %r, using default %d (minimum %d)",
            _ENV_MAX_OUTPUT_CHARS,
            raw,
            _DEFAULT_MAX_OUTPUT_CHARS,
            _MIN_MAX_CHARS,
        )
        return _DEFAULT_MAX_OUTPUT_CHARS
    return parsed

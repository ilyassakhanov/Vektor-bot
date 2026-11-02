"""Message I/O safety — inbound validation and outbound reply splitting.

Pure functions, no I/O and no Telegram/LLM imports. Enforcement happens at
the handler boundary (``bot.py``):

- ``validate_input`` rejects empty/blank and oversized inbound text and
  strips invisible zero-width and bidi control characters.
- ``split_reply`` splits an outgoing reply into ordered chunks that each fit
  Telegram's message limit, measured in UTF-16 code units.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("vektor.agent.validation")

TELEGRAM_MESSAGE_LIMIT = 4096
"""Telegram's message-length limit (a protocol constant, not configurable)."""

# Zero-width (U+200B–U+200D, U+2060, U+FEFF) and bidi control characters
# (U+202A–U+202E, U+2066–U+2069). Stripping U+200D (ZWJ) degrades ZWJ emoji
# sequences to their component emoji — an accepted trade-off (see design.md).
_INVISIBLE = frozenset(
    [
        *map(chr, range(0x200B, 0x200D + 1)),
        "\u2060",
        "\ufeff",
        *map(chr, range(0x202A, 0x202E + 1)),
        *map(chr, range(0x2066, 0x2069 + 1)),
    ]
)
_STRIP_TABLE = {ord(c): None for c in _INVISIBLE}

_SENTENCE_ENDS = (". ", "! ", "? ")


@dataclass(frozen=True)
class Verdict:
    """Result of ``validate_input``.

    ``text`` is the scrubbed text when ``ok`` (``""`` otherwise); ``reason``
    is ``""`` when ``ok``, else a stable token: ``"empty"`` or ``"too_long"``.
    """

    ok: bool
    text: str
    reason: str


def validate_input(text: str) -> Verdict:
    """Validate an inbound text message.

    Order: raw length > ``TELEGRAM_MESSAGE_LIMIT`` chars → ``too_long``;
    strip invisible characters; blank result → ``empty``; otherwise ok with
    the scrubbed text (surrounding whitespace is kept).
    """
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        return Verdict(ok=False, text="", reason="too_long")
    stripped = text.translate(_STRIP_TABLE)
    if not stripped.strip():
        return Verdict(ok=False, text="", reason="empty")
    return Verdict(ok=True, text=stripped, reason="")


def _units(ch: str) -> int:
    """UTF-16 code units for one character (2 for astral code points)."""
    return 2 if ord(ch) > 0xFFFF else 1


def _fit_end(text: str, limit: int) -> int:
    """Largest char index ``end`` with ``text[:end]`` ≤ ``limit`` UTF-16 units.

    Always at least 1 so splitting makes progress even when a single astral
    character exceeds a (pathological) limit of 1.
    """
    total = 0
    for i, ch in enumerate(text):
        total += _units(ch)
        if total > limit:
            return max(i, 1)
    return len(text)


def _cut_point(window: str) -> int:
    """Best cut index in ``window``: paragraph → sentence → word → hard cut.

    The separator stays with the left chunk, so nothing is dropped.
    """
    idx = window.rfind("\n\n")
    if idx > 0:
        return idx + 2
    sentence = max(
        [
            window.rfind(end) + len(end)
            for end in _SENTENCE_ENDS
            if window.rfind(end) >= 0
        ]
        + [window.rfind("\n") + 1]
    )
    if sentence > 0:
        return sentence
    for i in range(len(window) - 1, -1, -1):
        if window[i].isspace():
            return i + 1
    return len(window)


def split_reply(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into ordered chunks of at most ``limit`` UTF-16 units.

    A text that fits is returned as ``[text]`` (including ``""``). Cuts happen
    on ``str`` indices, so a surrogate pair is never split, and
    ``"".join(chunks) == text`` always holds.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    chunks: list[str] = []
    rest = text
    while True:
        end = _fit_end(rest, limit)
        if end == len(rest):
            chunks.append(rest)
            break
        cut = _cut_point(rest[:end])
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if len(chunks) > 1:
        log.debug("reply split into %d chunks", len(chunks))
    return chunks

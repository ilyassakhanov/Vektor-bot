"""Text chunking — whitespace-friendly fixed-size windows with overlap.

chunk_text normalizes input (words joined with single spaces, outer
whitespace stripped) and slices it into chunks of at most `size` characters.
Chunks are filled greedily word by word; when the next word would overflow,
the chunk is emitted and the next one starts from the previous chunk's
maximal word-aligned tail whose joined length does not exceed `overlap`.
Consecutive chunks therefore share approximately `overlap` characters of
trailing/leading content, word-aligned, and no word is ever split.

The one documented exception to the size cap: a single word longer than
`size` is emitted as its own (oversized) chunk rather than being split,
because word integrity takes precedence.

Every emitted chunk contains at least one word the previous chunk did not
end on, so windows always advance and chunking terminates. Word sequences of
consecutive chunks are contiguous slices of the input word list (modulo the
shared tail), so chunks reassemble the original text in order.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PageChunk:
    """One chunk plus the page it came from (``None`` when pages are unknown)."""

    page: int | None
    text: str


def chunk_pages(
    pages: list[tuple[int, str]], size: int = 800, overlap: int = 100
) -> list[PageChunk]:
    """Chunk each page independently; a chunk never spans two pages.

    Same window params (and validation) as :func:`chunk_text`; empty pages
    contribute no chunks; page numbers pass through unchanged.
    """
    return [
        PageChunk(page=page, text=chunk)
        for page, text in pages
        for chunk in chunk_text(text, size, overlap)
    ]


def chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    """Split ``text`` into fixed-size, word-aligned, overlapping chunks.

    Args:
        text: arbitrary text; whitespace is normalized (words joined with
            single spaces, outer whitespace stripped) before windowing.
        size: maximum chunk length in characters. Must be >= 1.
        overlap: maximum number of trailing characters (word-aligned) shared
            between consecutive chunks. Must satisfy 0 <= overlap < size.

    Returns:
        Chunks of at most ``size`` characters each (a single word longer
        than ``size`` becomes its own oversized chunk — words are never
        split; the word-aligned overlap tail is likewise trimmed when the
        incoming word would push the next chunk over ``size``). Empty or
        whitespace-only input yields an empty list; text that fits within
        ``size`` yields one stripped chunk. No chunk is ever empty.

    Raises:
        ValueError: if ``size < 1`` or ``overlap`` is negative or >= ``size``.
    """
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    if not 0 <= overlap < size:
        raise ValueError(f"overlap must satisfy 0 <= overlap < size, got {overlap}")

    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        joined_len = current_len + (1 if current else 0) + len(word)
        if current and joined_len > size:
            chunks.append(" ".join(current))
            current = _overlap_tail(current, overlap) + [word]
            while len(" ".join(current)) > size and len(current) > 1:
                current.pop(0)
            current_len = len(" ".join(current))
        else:
            current.append(word)
            current_len = joined_len
    if current:
        chunks.append(" ".join(current))
    return chunks


def _overlap_tail(chunk: list[str], overlap: int) -> list[str]:
    """Return the maximal trailing words whose joined length fits ``overlap``."""
    tail: list[str] = []
    tail_len = 0
    for word in reversed(chunk):
        extended = len(word) if not tail else tail_len + 1 + len(word)
        if extended > overlap:
            break
        tail.insert(0, word)
        tail_len = extended
    return tail

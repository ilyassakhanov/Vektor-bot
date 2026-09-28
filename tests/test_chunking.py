"""Tests for chunk_text — whitespace-friendly fixed-size windows with overlap.

chunk_text splits normalized text (single-space joined) into chunks of at most
`size` characters, carrying a word-aligned tail of at most `overlap` characters
from the previous chunk into the next one. Words are never split; a single
word longer than `size` is the documented exception (own, oversized chunk).

Covered acceptance criteria:
1. Input validation: size >= 1 and 0 <= overlap < size, else ValueError.
2. Empty / whitespace-only text -> []; text fitting within size -> one
   stripped chunk.
3. Window arithmetic verified exactly on a hand-computed example.
4. No chunk longer than size (except the documented oversized-word case), no
   empty chunks, full in-order word coverage via contiguous windows.
5. Consecutive chunks share a maximal word-aligned overlap of at most
   `overlap` characters; overlap=0 yields disjoint chunks.
6. Words are never split; re-ingest-safe normalization joins with single
   spaces; no duplicate consecutive chunks when text <= size (+overlap).
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from retrieval.chunking import chunk_text


def _numbered_words(count: int) -> list[str]:
    """Unique zero-padded words of uniform length so word -> index is exact."""
    width = max(2, len(str(count - 1)))
    return [f"w{str(i).zfill(width)}" for i in range(count)]


class TestValidation:
    @pytest.mark.parametrize(
        ("size", "overlap"),
        [(0, 0), (-5, 0), (-5, -1), (10, -1), (10, 10), (10, 11), (1, 1)],
    )
    def test_invalid_args_raise(self, size: int, overlap: int) -> None:
        with pytest.raises(ValueError):
            chunk_text("some text", size=size, overlap=overlap)

    @pytest.mark.parametrize(
        ("size", "overlap"), [(1, 0), (2, 0), (2, 1), (10, 0), (10, 9), (800, 100)]
    )
    def test_valid_args_never_raise(self, size: int, overlap: int) -> None:
        assert isinstance(chunk_text("a b c", size=size, overlap=overlap), list)


class TestEmptyAndShortInput:
    def test_empty_text_returns_empty_list(self) -> None:
        assert chunk_text("") == []

    @pytest.mark.parametrize("junk", ["   ", "\t\n  \n", " \t "])
    def test_whitespace_only_returns_empty_list(self, junk: str) -> None:
        assert chunk_text(junk) == []

    def test_short_text_single_stripped_chunk(self) -> None:
        assert chunk_text("  hello world  ") == ["hello world"]

    def test_text_exactly_size_single_chunk(self) -> None:
        text = "ab cd ef gh"
        assert len(text) == 11
        assert chunk_text(text, size=11, overlap=4) == ["ab cd ef gh"]

    def test_text_just_under_size_plus_overlap_no_duplicates(self) -> None:
        text = "aaa bbb ccc ddd"
        assert len(text) == 15
        chunks = chunk_text(text, size=10, overlap=8)
        assert len(set(chunks)) == len(chunks)
        assert all(chunks)


class TestWindowArithmetic:
    def test_exact_hand_computed_windows(self) -> None:
        chunks = chunk_text("aa bb cc dd ee ff gg", size=11, overlap=5)
        assert chunks == ["aa bb cc dd", "cc dd ee ff", "ee ff gg"]

    def test_consecutive_chunks_share_word_aligned_overlap(self) -> None:
        words = _numbered_words(40)
        chunks = chunk_text(" ".join(words), size=20, overlap=8)
        for previous, current in pairwise(chunks):
            previous_words = previous.split()
            previous_start = int(previous_words[0][1:])
            previous_end = previous_start + len(previous_words)
            current_start = int(current.split()[0][1:])
            shared = " ".join(words[current_start:previous_end])
            assert shared
            assert previous.endswith(shared)
            assert current.startswith(shared)
            assert len(shared) <= 8
            extended = " ".join(words[current_start - 1 : previous_end])
            assert len(extended) > 8

    def test_zero_overlap_yields_disjoint_chunks(self) -> None:
        chunks = chunk_text("aa bb cc dd ee ff gg", size=11, overlap=0)
        assert chunks == ["aa bb cc dd", "ee ff gg"]
        for previous, current in pairwise(chunks):
            assert set(previous.split()).isdisjoint(current.split())

    @pytest.mark.parametrize(
        ("size", "overlap"), [(20, 8), (11, 5), (10, 0), (13, 12), (5, 4), (25, 1)]
    )
    def test_chunks_respect_size_and_are_non_empty(
        self, size: int, overlap: int
    ) -> None:
        chunks = chunk_text(" ".join(_numbered_words(120)), size=size, overlap=overlap)
        assert chunks
        assert all(len(chunk) <= size for chunk in chunks)
        assert all(chunk.strip() for chunk in chunks)

    @pytest.mark.parametrize(
        ("size", "overlap"), [(20, 8), (11, 5), (10, 0), (13, 12), (5, 4), (25, 1)]
    )
    def test_windows_are_contiguous_and_cover_every_word(
        self, size: int, overlap: int
    ) -> None:
        words = _numbered_words(120)
        chunks = chunk_text(" ".join(words), size=size, overlap=overlap)

        windows: list[tuple[int, int]] = []
        for chunk in chunks:
            chunk_words = chunk.split()
            start = int(chunk_words[0][1:])
            assert chunk_words == words[start : start + len(chunk_words)]
            windows.append((start, start + len(chunk_words)))

        assert windows[0][0] == 0
        assert windows[-1][1] == len(words)
        for (start_previous, end_previous), (start_current, end_current) in pairwise(
            windows
        ):
            assert start_previous < start_current
            assert start_current <= end_previous
            assert end_current > end_previous

    @pytest.mark.parametrize(("size", "overlap"), [(20, 8), (11, 5), (25, 1)])
    def test_overlap_is_maximal_word_aligned_prefix(
        self, size: int, overlap: int
    ) -> None:
        chunks = chunk_text(" ".join(_numbered_words(120)), size=size, overlap=overlap)
        for previous, current in pairwise(chunks):
            previous_words = previous.split()
            current_words = current.split()
            shared = 0
            while shared < len(previous_words) and shared < len(current_words):
                if previous_words[-1 - shared] != current_words[shared]:
                    break
                shared += 1
            assert len(" ".join(current_words[:shared])) <= overlap
            whole_chunk_carried = shared == len(previous_words)
            if not whole_chunk_carried and shared < len(previous_words):
                extended = len(" ".join(previous_words[-1 - shared :]))
                assert extended > overlap


class TestWordIntegrity:
    def test_word_never_split(self) -> None:
        tokens = ["alpha", "beta:", "gamma_delta", "epsilon", "zeta"]
        text = "alpha  beta:   gamma_delta\n\tepsilon zeta"
        chunks = chunk_text(text, size=12, overlap=4)
        for token in tokens:
            assert any(token in chunk for chunk in chunks)

    def test_chunk_tokens_come_from_original_words(self) -> None:
        tokens = {"alpha", "beta:", "gamma_delta", "epsilon", "zeta"}
        chunks = chunk_text(
            "alpha  beta:   gamma_delta\n\tepsilon zeta", size=12, overlap=4
        )
        assert all(set(chunk.split()) <= tokens for chunk in chunks)

    def test_oversized_word_gets_own_chunk(self) -> None:
        chunks = chunk_text("abcdefghij short words", size=5, overlap=2)
        assert chunks == ["abcdefghij", "short", "words"]

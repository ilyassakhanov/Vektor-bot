"""Tests for RRF fusion — pure reciprocal rank fusion over multiple rankings.

rrf_fuse takes per-source best-first rankings of ChunkHit and merges them into
a deterministic fused list. Score contribution is 1/(k + rank) with 1-based
rank; raw source scores are never mixed; ties break by chunk_id.

Covered acceptance criteria:
1. Formula exact for a hit appearing in one source only.
2. A hit in two sources gets the sum of both contributions.
3. A duplicate within one source's list is merged with combined score.
4. Equal fused scores order deterministically by chunk_id.
5. top_k is respected.
6. Empty rankings (no sources, or all-empty lists) yield an empty list.
7. FusedHit.sources contains all contributing source names, sorted.
"""

from __future__ import annotations

import pytest

from retrieval.rrf import ChunkHit, FusedHit, rrf_fuse


def _hit(chunk_id: str, idx: int = 0, score: float = 1.0) -> ChunkHit:
    """Build a minimal ChunkHit for tests; content fields vary per chunk_id."""
    return ChunkHit(
        chunk_id=chunk_id,
        doc_id=f"doc-{chunk_id}",
        title=f"Title {chunk_id}",
        idx=idx,
        content=f"content of {chunk_id}",
        score=score,
    )


class TestRrfFuse:
    def test_single_source_exact_formula(self) -> None:
        rankings = {"vector": [_hit("a"), _hit("b"), _hit("c")]}

        fused = rrf_fuse(rankings, k=60, top_k=5)

        by_id = {f.chunk_id: f for f in fused}
        assert by_id["a"].score == pytest.approx(1 / 61)
        assert by_id["b"].score == pytest.approx(1 / 62)
        assert by_id["c"].score == pytest.approx(1 / 63)

    def test_two_sources_sum_contributions(self) -> None:
        rankings = {
            "vector": [_hit("a"), _hit("b")],
            "fts": [_hit("b"), _hit("a")],
        }

        fused = rrf_fuse(rankings, k=60, top_k=5)

        by_id = {f.chunk_id: f for f in fused}
        assert by_id["a"].score == pytest.approx(1 / 61 + 1 / 62)
        assert by_id["b"].score == pytest.approx(1 / 62 + 1 / 61)
        assert fused[0].chunk_id == "a"

    def test_duplicate_within_one_source_merged(self) -> None:
        rankings = {"vector": [_hit("a"), _hit("b"), _hit("a")]}

        fused = rrf_fuse(rankings, k=60, top_k=5)

        hits = [f for f in fused if f.chunk_id == "a"]
        assert len(hits) == 1
        assert hits[0].score == pytest.approx(1 / 61 + 1 / 63)

    def test_equal_scores_tie_break_by_chunk_id(self) -> None:
        rankings = {"s1": [_hit("z")], "s2": [_hit("m")], "s3": [_hit("a")]}

        fused = rrf_fuse(rankings, k=60, top_k=5)

        assert [f.chunk_id for f in fused] == ["a", "m", "z"]

    def test_top_k_respected(self) -> None:
        rankings = {
            "vector": [_hit(f"c{i}") for i in range(8)],
            "fts": [_hit(f"c{i}") for i in reversed(range(8))],
        }

        fused = rrf_fuse(rankings, k=60, top_k=3)

        assert len(fused) == 3
        all_fused = rrf_fuse(rankings, k=60, top_k=20)
        assert len(all_fused) == 8
        assert fused == all_fused[:3]

    def test_empty_rankings(self) -> None:
        assert rrf_fuse({}, k=60, top_k=5) == []
        assert rrf_fuse({"vector": [], "fts": []}, k=60, top_k=5) == []

    def test_sources_tuple_contains_all_contributing_sources(self) -> None:
        rankings = {
            "vector": [_hit("a"), _hit("b")],
            "fts": [_hit("b"), _hit("a"), _hit("c")],
        }

        fused = rrf_fuse(rankings, k=60, top_k=5)

        by_id = {f.chunk_id: f for f in fused}
        assert by_id["a"].sources == ("fts", "vector")
        assert by_id["b"].sources == ("fts", "vector")
        assert by_id["c"].sources == ("fts",)

    def test_metadata_taken_from_first_seen_hit(self) -> None:
        first = ChunkHit(
            chunk_id="a",
            doc_id="doc-1",
            title="First",
            idx=3,
            content="first content",
            score=0.9,
        )
        second = ChunkHit(
            chunk_id="a",
            doc_id="doc-2",
            title="Second",
            idx=7,
            content="second content",
            score=0.1,
        )
        rankings = {"vector": [first], "fts": [second]}

        fused = rrf_fuse(rankings, k=60, top_k=5)

        assert len(fused) == 1
        hit = fused[0]
        assert hit.doc_id == "doc-1"
        assert hit.title == "First"
        assert hit.idx == 3
        assert hit.content == "first content"

    def test_fused_hit_shape(self) -> None:
        fused = rrf_fuse({"vector": [_hit("a")]}, k=60, top_k=5)

        assert len(fused) == 1
        hit = fused[0]
        assert isinstance(hit, FusedHit)
        assert hit.chunk_id == "a"
        assert isinstance(hit.sources, tuple)
        assert isinstance(hit.score, float)

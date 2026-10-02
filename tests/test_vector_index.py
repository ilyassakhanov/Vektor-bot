"""Tests for VectorIndex — numpy cosine similarity search over float32 BLOBs.

VectorIndex decodes chunk_id -> embedding BLOB mappings (as produced by
ChunkStore.all_vectors) into a float32 matrix and answers cosine-similarity
queries with deterministic, best-first ChunkHit ordering. ``search`` takes a
LIST of query vectors and scores each chunk by MAX cosine similarity across
them (standard multi-query max-sim); a single-query list reproduces the plain
cosine ranking.

Covered acceptance criteria:
1. Cosine ordering correct with fake embeddings: query aligned with one
   vector, orthogonal to another, opposite to a third.
2. Zero vectors score exactly 0.0 (never NaN); a zero query scores 0.0 for
   every chunk with chunk_id ascending order.
3. Limit respected (limit 0, limit < n, limit > n).
4. Deterministic tie-break by ascending chunk_id for identical scores,
   independent of insertion order.
5. BLOB float32 round-trip lossless: dyadic values whose cosine arithmetic is
   exact in float32 yield exactly 1.0 / 0.0 / -1.0.
6. Empty index -> []; dimension mismatch between any query vector and stored
   vectors raises ValueError.
7. replace_all refreshes the index: new vectors searchable, old ids gone.
8. Hits carry chunk_id + score only; metadata fields stay empty for the later
   store join.
9. Multi-query max-sim: a chunk closest to ANY query vector wins; a
   single-query list ranks exactly like the legacy single-vector search; an
   empty query list returns [].
"""

from __future__ import annotations

import math
import threading

import numpy as np
import pytest

from retrieval.vector_index import VectorIndex, to_blob


def _blob(vector: list[float]) -> bytes:
    """Encode a Python list as a float32 BLOB, mirroring the embedder output."""
    return np.asarray(vector, dtype=np.float32).tobytes()


class TestCosineOrdering:
    def test_aligned_orthogonal_opposite_ordering(self) -> None:
        index = VectorIndex(
            {
                "c-aligned": _blob([2.0, 0.0, 0.0]),
                "d-orthogonal": _blob([0.0, 5.0, 0.0]),
                "a-opposite": _blob([-3.0, 0.0, 0.0]),
                "b-oblique": _blob([1.0, 1.0, 0.0]),
            }
        )

        hits = index.search([[1.0, 0.0, 0.0]], limit=10)

        assert [hit.chunk_id for hit in hits] == [
            "c-aligned",
            "b-oblique",
            "d-orthogonal",
            "a-opposite",
        ]
        assert hits[0].score == pytest.approx(1.0)
        assert hits[1].score == pytest.approx(1.0 / math.sqrt(2.0), rel=1e-6)
        assert hits[2].score == pytest.approx(0.0, abs=1e-7)
        assert hits[3].score == pytest.approx(-1.0)

    def test_magnitudes_do_not_matter(self) -> None:
        index = VectorIndex(
            {
                "tiny": _blob([0.001, 0.0]),
                "huge": _blob([1000.0, 0.0]),
            }
        )

        hits = index.search([[3.0, 0.0]], limit=2)

        assert hits[0].score == pytest.approx(hits[1].score)
        assert not math.isnan(hits[0].score)


class TestMultiQueryMaxSim:
    def test_chunk_closest_to_any_query_wins(self) -> None:
        """A chunk mediocre for query 1 but perfect for query 2 must win.

        For query 1 alone, "med" (0.8) outranks "best2" (0.0); max-sim lifts
        "best2" to 1.0 via query 2.
        """
        index = VectorIndex(
            {
                "med": _blob([0.8, 0.6]),
                "best2": _blob([0.0, 1.0]),
                "best1": _blob([1.0, 0.0]),
            }
        )

        hits = index.search([[1.0, 0.0], [0.0, 1.0]], limit=3)

        assert [hit.chunk_id for hit in hits] == ["best1", "best2", "med"]
        assert hits[0].score == pytest.approx(1.0)
        assert hits[1].score == pytest.approx(1.0)
        assert hits[2].score == pytest.approx(0.8)

    def test_single_query_list_matches_legacy_single_vector_search(self) -> None:
        index = VectorIndex(
            {
                "c-aligned": _blob([2.0, 0.0, 0.0]),
                "d-orthogonal": _blob([0.0, 5.0, 0.0]),
                "a-opposite": _blob([-3.0, 0.0, 0.0]),
                "b-oblique": _blob([1.0, 1.0, 0.0]),
            }
        )

        hits = index.search([[1.0, 0.0, 0.0]], limit=10)

        assert [hit.chunk_id for hit in hits] == [
            "c-aligned",
            "b-oblique",
            "d-orthogonal",
            "a-opposite",
        ]
        assert hits[0].score == pytest.approx(1.0)
        assert hits[3].score == pytest.approx(-1.0)

    def test_zero_query_in_list_does_not_mask_other_queries(self) -> None:
        index = VectorIndex({"pos": _blob([1.0, 0.0]), "neg": _blob([-1.0, 0.0])})

        hits = index.search([[0.0, 0.0], [1.0, 0.0]], limit=2)

        assert [hit.chunk_id for hit in hits] == ["pos", "neg"]
        assert hits[0].score == pytest.approx(1.0)
        assert hits[1].score == 0.0
        assert not math.isnan(hits[1].score)

    def test_empty_query_list_returns_empty(self) -> None:
        index = VectorIndex({"a": _blob([1.0, 0.0])})

        assert index.search([], limit=5) == []

    def test_empty_inner_query_vector_raises(self) -> None:
        index = VectorIndex({"a": _blob([1.0, 0.0])})
        with pytest.raises(ValueError):
            index.search([[]], limit=1)


class TestZeroVector:
    def test_stored_zero_vector_scores_exactly_zero(self) -> None:
        index = VectorIndex({"zero": _blob([0.0, 0.0]), "pos": _blob([1.0, 0.0])})

        hits = index.search([[1.0, 0.0]], limit=2)

        assert [hit.chunk_id for hit in hits] == ["pos", "zero"]
        assert hits[1].score == 0.0
        assert not math.isnan(hits[1].score)

    def test_zero_query_scores_zero_for_all(self) -> None:
        index = VectorIndex({"b": _blob([1.0, 2.0]), "a": _blob([3.0, 4.0])})

        hits = index.search([[0.0, 0.0]], limit=5)

        assert [hit.chunk_id for hit in hits] == ["a", "b"]
        assert all(hit.score == 0.0 for hit in hits)
        assert not any(math.isnan(hit.score) for hit in hits)


class TestLimit:
    def test_limit_zero_returns_empty(self) -> None:
        index = VectorIndex({"a": _blob([1.0, 0.0])})
        assert index.search([[1.0, 0.0]], limit=0) == []

    def test_limit_below_collection_size(self) -> None:
        index = VectorIndex({f"v{i}": _blob([1.0, float(i)]) for i in range(4)})

        hits = index.search([[1.0, 3.0]], limit=2)

        assert len(hits) == 2
        assert hits[0].chunk_id == "v3"

    def test_limit_above_collection_size(self) -> None:
        index = VectorIndex({"a": _blob([1.0, 0.0])})
        assert len(index.search([[1.0, 0.0]], limit=99)) == 1


class TestTieBreak:
    def test_identical_vectors_order_by_chunk_id(self) -> None:
        index = VectorIndex({"zz": _blob([1.0, 2.0]), "aa": _blob([1.0, 2.0])})

        hits = index.search([[1.0, 2.0]], limit=2)

        assert [hit.chunk_id for hit in hits] == ["aa", "zz"]
        assert hits[0].score == pytest.approx(1.0)
        assert hits[1].score == pytest.approx(1.0)

    def test_repeated_search_is_deterministic(self) -> None:
        index = VectorIndex(
            {"m": _blob([1.0, 1.0]), "b": _blob([1.0, 0.0]), "q": _blob([0.0, 1.0])}
        )

        first = index.search([[1.0, 1.0]], limit=3)
        second = index.search([[1.0, 1.0]], limit=3)

        assert first == second


class TestBlobRoundTrip:
    def test_blob_bytes_survive_float32_round_trip(self) -> None:
        vector = [1.5, -2.0, 0.25]
        blob = np.asarray(vector, dtype=np.float32).tobytes()

        decoded = np.frombuffer(blob, dtype=np.float32)

        assert list(decoded) == vector
        assert decoded.tobytes() == blob

    def test_dyadic_vectors_score_exactly(self) -> None:
        index = VectorIndex(
            {
                "same": _blob([1.5, -2.0]),
                "parallel": _blob([3.0, -4.0]),
                "orthogonal": _blob([2.0, 1.5]),
                "opposite": _blob([-1.5, 2.0]),
            }
        )

        hits = index.search([[1.5, -2.0]], limit=4)

        scores = {hit.chunk_id: hit.score for hit in hits}
        assert scores["same"] == 1.0
        assert scores["parallel"] == 1.0
        assert scores["orthogonal"] == 0.0
        assert scores["opposite"] == -1.0


class TestEmptyIndexAndDimensions:
    def test_empty_index_returns_empty_list(self) -> None:
        assert VectorIndex().search([[1.0, 0.0]], limit=5) == []

    def test_empty_mapping_returns_empty_list(self) -> None:
        assert VectorIndex({}).search([[1.0, 0.0]], limit=5) == []

    def test_dimension_mismatch_raises(self) -> None:
        index = VectorIndex({"a": _blob([1.0, 0.0])})
        with pytest.raises(ValueError):
            index.search([[1.0, 0.0, 0.0]], limit=1)

    def test_dimension_checked_against_current_vectors_after_replace(self) -> None:
        index = VectorIndex({"a": _blob([1.0, 0.0])})
        index.replace_all({"b": _blob([1.0, 0.0, 0.0])})
        with pytest.raises(ValueError):
            index.search([[1.0, 0.0]], limit=1)


class TestRefresh:
    def test_replace_all_swaps_vectors_atomically(self) -> None:
        index = VectorIndex({"old-a": _blob([1.0, 0.0]), "old-b": _blob([0.0, 1.0])})

        index.replace_all({"new-c": _blob([0.0, 1.0])})

        hits = index.search([[0.0, 1.0]], limit=10)
        assert [hit.chunk_id for hit in hits] == ["new-c"]
        assert hits[0].score == pytest.approx(1.0)

    def test_replaced_away_ids_are_gone(self) -> None:
        index = VectorIndex({"old-a": _blob([1.0, 0.0])})
        index.replace_all({"new-c": _blob([1.0, 0.0])})

        hits = index.search([[1.0, 0.0]], limit=10)

        assert [hit.chunk_id for hit in hits] == ["new-c"]

    def test_replace_all_with_empty_clears_index(self) -> None:
        index = VectorIndex({"a": _blob([1.0, 0.0])})
        index.replace_all({})
        assert index.search([[1.0, 0.0]], limit=5) == []


class TestHitMetadata:
    def test_hits_carry_only_chunk_id_and_score(self) -> None:
        index = VectorIndex({"a": _blob([1.0, 0.0])})

        hit = index.search([[1.0, 0.0]], limit=1)[0]

        assert hit.chunk_id == "a"
        assert hit.score == pytest.approx(1.0)
        assert hit.doc_id == ""
        assert hit.title == ""
        assert hit.idx == 0
        assert hit.content == ""


class TestToBlob:
    def test_serializes_list_as_float32(self) -> None:
        vector = [1.5, -2.0, 0.25]
        assert to_blob(vector) == np.asarray(vector, dtype=np.float32).tobytes()

    def test_accepts_ndarray(self) -> None:
        assert to_blob(np.asarray([1.0, 2.0], dtype=np.float64)) == to_blob([1.0, 2.0])

    def test_round_trips_through_frombuffer(self) -> None:
        vector = [0.5, -0.25, 3.0]
        assert list(np.frombuffer(to_blob(vector), dtype=np.float32)) == vector

    def test_feeds_vector_index_search(self) -> None:
        index = VectorIndex({"a": to_blob([1.0, 0.0])})
        assert index.search([[1.0, 0.0]], limit=1)[0].chunk_id == "a"


class TestConcurrentSwap:
    def test_search_during_replace_all_never_mixes_generations(self) -> None:
        """Torn reads must be impossible: cross-dimension generations.

        Generation A is dim-4 (8 ids), generation B dim-2 (3 ids), and the
        searcher always queries with the dim-4 vector. Against a fully
        consistent state the query either succeeds (generation A — only
        a-prefixed ids are possible) or raises the documented
        dimension-mismatch ValueError (generation B). A torn ids/matrix
        read from a non-atomic replace_all can satisfy neither: mismatched
        shapes surface as numpy shape errors (a different ValueError), and
        an ids-only tear surfaces as wrong-generation ids in a successful
        result. Both are recorded as failures.
        """
        gen_a = {f"a{i}": _blob([1.0, float(i), 0.0, 0.0]) for i in range(8)}
        gen_b = {f"b{i}": _blob([1.0, float(i)]) for i in range(3)}
        index = VectorIndex(gen_a)
        query = [[1.0, 2.0, 0.0, 0.0]]

        illegal: list[str] = []
        stop = threading.Event()

        def searcher() -> None:
            while not stop.is_set():
                try:
                    hits = index.search(query, limit=10)
                except ValueError as exc:
                    if "does not match" not in str(exc):
                        illegal.append(f"raised ValueError: {exc}")
                    continue
                except Exception as exc:  # noqa: BLE001
                    illegal.append(f"raised {type(exc).__name__}: {exc}")
                    return
                wrong = [
                    hit.chunk_id for hit in hits if not hit.chunk_id.startswith("a")
                ]
                if wrong:
                    illegal.append(f"dim-4 query returned {wrong}")

        threads = [threading.Thread(target=searcher) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            for _ in range(600):
                index.replace_all(gen_a)
                index.replace_all(gen_b)
        finally:
            stop.set()
            for thread in threads:
                thread.join()

        assert not illegal

"""Offline tests for the retrieval benchmark scoring math and dataset.

Only the pure scoring functions and the inline labeled dataset are tested —
no network, no Ollama, no store. The live runner in
``benchmarks/retrieval_bench.py`` executes exclusively under ``main()`` /
``__main__`` and is excluded from pytest via ``pytest.ini`` (testpaths =
tests).
"""

from __future__ import annotations

import pytest

from benchmarks.retrieval_bench import (
    CORPUS,
    QUERIES,
    precision_at_k,
    recall_at_k,
    score_run,
)


class TestRecallAtK:
    def test_perfect_retrieval_is_one(self) -> None:
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0

    def test_partial_retrieval_is_fraction_of_relevant(self) -> None:
        assert recall_at_k(["a", "x", "b"], {"a", "b", "c"}, 3) == pytest.approx(
            2.0 / 3.0
        )

    def test_k_truncation_excludes_hits_beyond_k(self) -> None:
        assert recall_at_k(["a", "b", "c"], {"c"}, 2) == 0.0
        assert recall_at_k(["a", "b", "c"], {"a"}, 1) == 1.0

    def test_empty_relevant_is_zero_by_definition(self) -> None:
        assert recall_at_k(["a", "b"], set(), 2) == 0.0

    def test_empty_retrieved_is_zero(self) -> None:
        assert recall_at_k([], {"a"}, 3) == 0.0

    def test_non_positive_k_is_zero(self) -> None:
        assert recall_at_k(["a"], {"a"}, 0) == 0.0

    def test_duplicate_retrieved_ids_are_deduped(self) -> None:
        assert recall_at_k(["a", "a", "b"], {"a", "b"}, 3) == 1.0


class TestPrecisionAtK:
    def test_perfect_retrieval_at_k_one(self) -> None:
        assert precision_at_k(["a"], {"a"}, 1) == 1.0

    def test_denominator_is_k(self) -> None:
        assert precision_at_k(["a"], {"a"}, 3) == pytest.approx(1.0 / 3.0)

    def test_k_truncation_only_top_k_count(self) -> None:
        assert precision_at_k(["a", "b", "x"], {"a", "b"}, 2) == 1.0
        assert precision_at_k(["x", "a", "b"], {"a", "b"}, 2) == pytest.approx(0.5)

    def test_empty_retrieved_is_zero(self) -> None:
        assert precision_at_k([], {"a"}, 3) == 0.0

    def test_non_positive_k_is_zero(self) -> None:
        assert precision_at_k(["a"], {"a"}, 0) == 0.0

    def test_duplicates_occupy_k_slots(self) -> None:
        # "a" twice fills two of three slots — only one unique relevant hit.
        assert precision_at_k(["a", "a", "b"], {"a", "b"}, 3) == pytest.approx(
            2.0 / 3.0
        )


class TestScoreRun:
    def test_row_combines_hits_recall_precision(self) -> None:
        row = score_run(["a", "x"], {"a", "b"}, 2)
        assert row["relevant_hits"] == 1
        assert row["recall"] == pytest.approx(0.5)
        assert row["precision"] == pytest.approx(0.5)

    def test_row_empty_retrieval(self) -> None:
        row = score_run([], {"a"}, 2)
        assert row["relevant_hits"] == 0
        assert row["recall"] == 0.0
        assert row["precision"] == 0.0


class TestDataset:
    def test_corpus_and_queries_are_non_empty(self) -> None:
        assert CORPUS
        assert QUERIES

    def test_corpus_doc_ids_are_unique(self) -> None:
        doc_ids = [doc_id for doc_id, _text in CORPUS]
        assert len(doc_ids) == len(set(doc_ids))

    def test_every_relevant_id_references_a_corpus_doc(self) -> None:
        doc_ids = {doc_id for doc_id, _text in CORPUS}
        for _query, relevant in QUERIES:
            assert relevant <= doc_ids

    def test_at_least_one_query_has_multiple_relevant_docs(self) -> None:
        assert any(len(relevant) > 1 for _query, relevant in QUERIES)

    def test_corpus_texts_are_short_enough_for_one_chunk(self) -> None:
        # Each doc must stay a single chunk at half the default size, so the
        # chunk_id -> doc_id mapping used by the live runner stays 1:1.
        for _doc_id, text in CORPUS:
            assert len(text) < 400

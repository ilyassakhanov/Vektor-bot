"""Tests for HybridRetriever — fakes only, no network, no Ollama, no SQLite.

All searches run for an explicit ``user_id`` (SQL-level owner scoping below
this layer); the fakes record it with the rest of the call.
"""

from __future__ import annotations

import time

import pytest
from prometheus_client import REGISTRY

from llm.base import LLMError
from retrieval.embeddings import Embedder, EmbeddingError
from retrieval.expansion import QueryExpander
from retrieval.hybrid import FtsSearch, HybridResult, HybridRetriever, VectorSearch
from retrieval.rerank import Reranker
from retrieval.rrf import ChunkHit
from tests.fakes import FakeLLM

_USER = "user-1"


def _counter(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _hit(chunk_id: str, score: float = 1.0) -> ChunkHit:
    return ChunkHit(
        chunk_id=chunk_id,
        doc_id="doc-1",
        title="Doc",
        idx=0,
        content=f"content of {chunk_id}",
        score=score,
    )


class FakeEmbedder(Embedder):
    """Deterministic embedder: vector keyed on text length; optionally raises."""

    def __init__(self, error: EmbeddingError | None = None) -> None:
        self.calls: list[list[str]] = []
        self._error = error

    @staticmethod
    def vector_for(text: str) -> list[float]:
        return [float(len(text)), float(sum(ord(c) for c in text) % 97), 1.0]

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._error is not None:
            raise self._error
        return [self.vector_for(text) for text in texts]


class FakeFts(FtsSearch):
    """Scriptable FTS fake: records calls, sleeps, returns hits or raises."""

    def __init__(
        self,
        hits: list[ChunkHit] | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.calls: list[tuple[list[str], int, str]] = []
        self.entered = False
        self._hits = hits or []
        self._error = error
        self._delay = delay

    def search(self, terms: list[str], limit: int, user_id: str) -> list[ChunkHit]:
        self.entered = True
        self.calls.append((list(terms), limit, user_id))
        if self._delay:
            time.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return list(self._hits)


class FakeVector(VectorSearch):
    """Scriptable vector fake: records calls, sleeps, returns hits or raises."""

    def __init__(
        self,
        hits: list[ChunkHit] | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.calls: list[tuple[list[list[float]], int, str]] = []
        self.entered = False
        self._hits = hits or []
        self._error = error
        self._delay = delay

    def search(
        self, queries: list[list[float]], limit: int, user_id: str
    ) -> list[ChunkHit]:
        self.entered = True
        self.calls.append(([list(query) for query in queries], limit, user_id))
        if self._delay:
            time.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return list(self._hits)


def _retriever(
    embedder: Embedder,
    vector: VectorSearch,
    fts: FtsSearch | None,
    expander: QueryExpander | None = None,
    reranker: Reranker | None = None,
    **limits: int,
) -> HybridRetriever:
    return HybridRetriever(
        embedder=embedder,
        vector=vector,
        fts=fts,
        expander=expander,
        reranker=reranker,
        **limits,
    )


def test_happy_path_fuses_both_sources_with_exact_rrf_scores():
    vector = FakeVector(hits=[_hit("a"), _hit("b"), _hit("c")])
    fts = FakeFts(hits=[_hit("b"), _hit("a"), _hit("d")])

    result = _retriever(FakeEmbedder(), vector, fts, rrf_k=60, top_k=5).search(
        "hybrid search", _USER
    )

    assert isinstance(result, HybridResult)
    assert [hit.chunk_id for hit in result.hits] == ["a", "b", "c", "d"]
    assert result.hits[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert result.hits[1].score == pytest.approx(1 / 62 + 1 / 61)
    assert result.hits[2].score == pytest.approx(1 / 63)
    assert result.hits[3].score == pytest.approx(1 / 63)
    assert result.hits[0].sources == ("fts", "vector")
    assert result.hits[2].sources == ("vector",)
    assert result.sources_used == ("vector", "fts")
    assert result.used_expansion is False
    assert result.note == ""
    assert len(result.hits) <= 5


def test_limits_forwarded_to_sources():
    vector = FakeVector(hits=[_hit("a")])
    fts = FakeFts(hits=[_hit("a")])

    _retriever(FakeEmbedder(), vector, fts, vector_limit=7, fts_limit=9).search(
        "hybrid search", _USER
    )

    assert vector.calls[0][1] == 7
    assert fts.calls[0][1] == 9


def test_user_id_forwarded_to_both_sources():
    vector = FakeVector(hits=[_hit("a")])
    fts = FakeFts(hits=[_hit("a")])

    _retriever(FakeEmbedder(), vector, fts).search("hybrid search", _USER)

    assert vector.calls[0][2] == _USER
    assert fts.calls[0][2] == _USER


def test_sources_run_concurrently():
    vector = FakeVector(hits=[_hit("a")], delay=0.15)
    fts = FakeFts(hits=[_hit("b")], delay=0.15)

    start = time.monotonic()
    result = _retriever(FakeEmbedder(), vector, fts).search("hybrid search", _USER)
    elapsed = time.monotonic() - start

    assert vector.entered and fts.entered
    assert elapsed < 0.25
    assert len(result.hits) == 2


def test_expanded_terms_reach_both_sources():
    llm = FakeLLM(
        reply='{"keywords": ["vector search", "semantic retrieval"],'
        ' "queries": ["how does rrf work"]}'
    )
    embedder = FakeEmbedder()
    vector = FakeVector(hits=[_hit("a")])
    fts = FakeFts(hits=[_hit("a")])

    result = _retriever(embedder, vector, fts, expander=QueryExpander(llm)).search(
        "hybrid search", _USER
    )

    assert result.used_expansion is True
    assert fts.calls[0][0] == [
        "hybrid search",
        "vector search",
        "semantic retrieval",
        "how does rrf work",
    ]
    assert embedder.calls == [["hybrid search", "how does rrf work"]]
    assert vector.calls[0][0] == [
        FakeEmbedder.vector_for("hybrid search"),
        FakeEmbedder.vector_for("how does rrf work"),
    ]


def test_embed_batch_dedupes_and_drops_whitespace_and_skips_keywords():
    llm = FakeLLM(
        reply='{"keywords": ["vector search"],'
        ' "queries": ["HYBRID SEARCH", "   ", "how does rrf work"]}'
    )
    embedder = FakeEmbedder()
    vector = FakeVector(hits=[_hit("a")])
    fts = FakeFts(hits=[_hit("a")])

    _retriever(embedder, vector, fts, expander=QueryExpander(llm)).search(
        "hybrid search", _USER
    )

    assert embedder.calls == [["hybrid search", "how does rrf work"]]
    assert vector.calls[0][0] == [
        FakeEmbedder.vector_for("hybrid search"),
        FakeEmbedder.vector_for("how does rrf work"),
    ]


def test_duplicate_expanded_terms_are_deduped():
    llm = FakeLLM(reply='{"keywords": ["HYBRID SEARCH", "vector search"]}')
    fts = FakeFts(hits=[_hit("a")])

    _retriever(FakeEmbedder(), FakeVector(), fts, expander=QueryExpander(llm)).search(
        "hybrid search", _USER
    )

    assert fts.calls[0][0] == ["hybrid search", "vector search"]


def test_vector_failure_fts_still_answers():
    llm = FakeLLM(reply='{"keywords": ["vector search"]}')
    vector = FakeVector(error=RuntimeError("index gone"))
    fts = FakeFts(hits=[_hit("b"), _hit("a")])

    result = _retriever(
        FakeEmbedder(), vector, fts, expander=QueryExpander(llm)
    ).search("hybrid search", _USER)

    assert [hit.chunk_id for hit in result.hits] == ["b", "a"]
    assert result.hits[0].sources == ("fts",)
    assert result.sources_used == ("fts",)
    assert result.used_expansion is True
    assert "degraded" in result.note
    assert "vector" in result.note


def test_fts_failure_vector_still_answers():
    vector = FakeVector(hits=[_hit("a"), _hit("b")])
    fts = FakeFts(error=ValueError("fts boom"))

    result = _retriever(FakeEmbedder(), vector, fts).search("hybrid search", _USER)

    assert [hit.chunk_id for hit in result.hits] == ["a", "b"]
    assert result.sources_used == ("vector",)
    assert "degraded" in result.note
    assert "fts" in result.note


def test_fts_empty_result_is_not_a_failure():
    vector = FakeVector(hits=[_hit("a")])
    fts = FakeFts(hits=[])

    result = _retriever(FakeEmbedder(), vector, fts).search("hybrid search", _USER)

    assert [hit.chunk_id for hit in result.hits] == ["a"]
    assert result.sources_used == ("vector",)
    assert result.note == ""
    assert fts.entered


def test_embedding_error_degrades_to_fts_only():
    embedder = FakeEmbedder(error=EmbeddingError("ollama down"))
    vector = FakeVector(hits=[_hit("a")])
    fts = FakeFts(hits=[_hit("b")])

    result = _retriever(embedder, vector, fts).search("hybrid search", _USER)

    assert embedder.calls == [["hybrid search"]]
    assert vector.entered is False
    assert fts.entered
    assert [hit.chunk_id for hit in result.hits] == ["b"]
    assert result.sources_used == ("fts",)
    assert "degraded" in result.note
    assert "vector" in result.note


def test_both_sources_fail_returns_empty_result_with_note():
    vector = FakeVector(error=RuntimeError("index gone"))
    fts = FakeFts(error=ValueError("fts boom"))

    result = _retriever(FakeEmbedder(), vector, fts).search("hybrid search", _USER)

    assert result.hits == []
    assert result.sources_used == ()
    assert "degraded" in result.note
    assert "vector" in result.note
    assert "fts" in result.note


def test_embedding_error_and_fts_failure_returns_empty_result_with_note():
    embedder = FakeEmbedder(error=EmbeddingError("ollama down"))
    vector = FakeVector(hits=[_hit("a")])
    fts = FakeFts(error=LLMError("unrelated but fatal"))

    result = _retriever(embedder, vector, fts).search("hybrid search", _USER)

    assert result.hits == []
    assert result.sources_used == ()
    assert "degraded" in result.note


def test_no_expander_means_no_llm_call_and_original_query_everywhere():
    llm = FakeLLM(reply='{"keywords": ["never used"]}')
    embedder = FakeEmbedder()
    vector = FakeVector(hits=[_hit("a")])
    fts = FakeFts(hits=[_hit("a")])
    QueryExpander(llm)

    result = _retriever(embedder, vector, fts, expander=None).search(
        "hybrid search", _USER
    )

    assert llm.calls == []
    assert result.used_expansion is False
    assert embedder.calls == [["hybrid search"]]
    assert vector.calls[0][0] == [FakeEmbedder.vector_for("hybrid search")]
    assert fts.calls[0][0] == ["hybrid search"]


def test_fts_none_is_vector_only_mode_without_degradation():
    vector = FakeVector(hits=[_hit("a"), _hit("b")])

    result = _retriever(FakeEmbedder(), vector, fts=None).search("hybrid search", _USER)

    assert [hit.chunk_id for hit in result.hits] == ["a", "b"]
    assert result.sources_used == ("vector",)
    assert result.note == ""
    assert result.used_expansion is False


def test_top_k_smaller_than_available_hits_truncates():
    vector = FakeVector(hits=[_hit(cid) for cid in "abcde"])
    fts = FakeFts(hits=[_hit(cid) for cid in "fg"])

    result = _retriever(FakeEmbedder(), vector, fts, top_k=3, rrf_k=60).search(
        "hybrid search", _USER
    )

    assert [hit.chunk_id for hit in result.hits] == ["a", "f", "b"]
    assert result.hits[0].score == pytest.approx(1 / 61)
    assert result.hits[1].score == pytest.approx(1 / 61)
    assert result.hits[2].score == pytest.approx(1 / 62)


def test_reranker_reorders_fused_hits_and_records_ok():
    rerank_llm = FakeLLM(reply='{"scores": [1, 9, 5]}')
    vector = FakeVector(hits=[_hit("a"), _hit("b"), _hit("c")])
    ok_before = _counter("vektor_retrieval_rerank_total", {"status": "ok"})
    fallback_before = _counter("vektor_retrieval_rerank_total", {"status": "fallback"})
    latency_before = _counter(
        "vektor_retrieval_latency_seconds_count", {"stage": "rerank"}
    )

    result = _retriever(
        FakeEmbedder(), vector, fts=None, reranker=Reranker(rerank_llm)
    ).search("hybrid search", _USER)

    assert [hit.chunk_id for hit in result.hits] == ["b", "c", "a"]
    assert len(rerank_llm.calls) == 1
    assert _counter("vektor_retrieval_rerank_total", {"status": "ok"}) == ok_before + 1
    assert _counter("vektor_retrieval_rerank_total", {"status": "fallback"}) == (
        fallback_before
    )
    assert (
        _counter("vektor_retrieval_latency_seconds_count", {"stage": "rerank"})
        == latency_before + 1
    )


def test_rerank_failure_keeps_rrf_order_and_records_fallback():
    rerank_llm = FakeLLM(error=LLMError("timeout"))
    vector = FakeVector(hits=[_hit("a"), _hit("b")])
    ok_before = _counter("vektor_retrieval_rerank_total", {"status": "ok"})
    fallback_before = _counter("vektor_retrieval_rerank_total", {"status": "fallback"})
    latency_before = _counter(
        "vektor_retrieval_latency_seconds_count", {"stage": "rerank"}
    )

    result = _retriever(
        FakeEmbedder(), vector, fts=None, reranker=Reranker(rerank_llm)
    ).search("hybrid search", _USER)

    assert [hit.chunk_id for hit in result.hits] == ["a", "b"]
    assert _counter("vektor_retrieval_rerank_total", {"status": "ok"}) == ok_before
    assert _counter("vektor_retrieval_rerank_total", {"status": "fallback"}) == (
        fallback_before + 1
    )
    assert (
        _counter("vektor_retrieval_latency_seconds_count", {"stage": "rerank"})
        == latency_before + 1
    )


def test_no_reranker_skips_rerank_stage():
    vector = FakeVector(hits=[_hit("a"), _hit("b")])
    ok_before = _counter("vektor_retrieval_rerank_total", {"status": "ok"})
    fallback_before = _counter("vektor_retrieval_rerank_total", {"status": "fallback"})
    latency_before = _counter(
        "vektor_retrieval_latency_seconds_count", {"stage": "rerank"}
    )

    result = _retriever(FakeEmbedder(), vector, fts=None).search("hybrid search", _USER)

    assert [hit.chunk_id for hit in result.hits] == ["a", "b"]
    assert _counter("vektor_retrieval_rerank_total", {"status": "ok"}) == ok_before
    assert _counter("vektor_retrieval_rerank_total", {"status": "fallback"}) == (
        fallback_before
    )
    assert (
        _counter("vektor_retrieval_latency_seconds_count", {"stage": "rerank"})
        == latency_before
    )


def test_empty_fused_hits_skip_rerank_stage():
    rerank_llm = FakeLLM(reply='{"scores": [1]}')
    vector = FakeVector(hits=[])
    ok_before = _counter("vektor_retrieval_rerank_total", {"status": "ok"})
    fallback_before = _counter("vektor_retrieval_rerank_total", {"status": "fallback"})

    result = _retriever(
        FakeEmbedder(), vector, fts=None, reranker=Reranker(rerank_llm)
    ).search("hybrid search", _USER)

    assert result.hits == []
    assert rerank_llm.calls == []
    assert _counter("vektor_retrieval_rerank_total", {"status": "ok"}) == ok_before
    assert _counter("vektor_retrieval_rerank_total", {"status": "fallback"}) == (
        fallback_before
    )

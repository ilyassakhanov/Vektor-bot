"""Tests for Reranker — FakeLLM only, no network, no Ollama."""

from __future__ import annotations

from llm.base import LLM, ChatResponse, LLMError, LLMResponse, Message, ToolSpec
from retrieval.rerank import Reranker, RerankResult
from retrieval.rrf import FusedHit
from tests.fakes import CloseableLLM, FakeLLM


class ExplodingLLM(LLM):
    """LLM whose generate() raises an unexpected (non-LLMError) exception."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def generate(self, message: str) -> LLMResponse:
        raise self._error

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system: str = "",
    ) -> ChatResponse:
        raise self._error


def _hit(chunk_id: str, score: float = 1.0) -> FusedHit:
    return FusedHit(
        chunk_id=chunk_id,
        doc_id="doc-1",
        title="Doc",
        idx=0,
        content=f"passage text of {chunk_id}",
        score=score,
        sources=("vector",),
    )


def _rerank(
    reply: str, hits: list[FusedHit], top_k: int = 5, query: str = "hybrid search"
) -> RerankResult:
    return Reranker(FakeLLM(reply=reply)).rerank(query, list(hits), top_k)


def test_reorder_ok_with_json_scores():
    a, b = _hit("a"), _hit("b")

    result = _rerank('{"scores": [3, 9]}', [a, b])

    assert result.ok is True
    assert result.hits == [b, a]
    assert result.hits[0] is b


def test_one_llm_call_scores_all_hits_and_prompt_carries_query_and_passages():
    llm = FakeLLM(reply='{"scores": [1, 2]}')

    Reranker(llm).rerank("rrf fusion", [_hit("a"), _hit("b")], top_k=5)

    assert len(llm.calls) == 1
    assert "rrf fusion" in llm.calls[0]
    assert "passage text of a" in llm.calls[0]
    assert "passage text of b" in llm.calls[0]


def test_float_scores_are_accepted():
    a, b = _hit("a"), _hit("b")

    result = _rerank('{"scores": [8.5, 2.0]}', [a, b])

    assert result.ok is True
    assert result.hits == [a, b]


def test_ties_keep_input_rrf_order():
    a, b, c = _hit("a"), _hit("b"), _hit("c")

    result = _rerank('{"scores": [5, 5, 5]}', [a, b, c])

    assert result.hits == [a, b, c]


def test_missing_score_for_one_hit_sinks_it_others_reorder():
    a, b, c = _hit("a"), _hit("b"), _hit("c")

    result = _rerank('{"scores": [5, 9]}', [a, b, c])

    assert result.ok is True
    assert result.hits == [b, a, c]


def test_invalid_score_counts_zero_for_that_hit():
    a, b = _hit("a"), _hit("b")

    result = _rerank('{"scores": [2, "high"]}', [a, b])

    assert result.ok is True
    assert result.hits == [a, b]


def test_malformed_json_shaped_reply_falls_back_to_input_order():
    a, b = _hit("a"), _hit("b")

    result = _rerank('{"scores": "nope"}', [a, b])

    assert result.ok is False
    assert result.hits == [a, b]


def test_unparseable_garbage_falls_back_to_input_order():
    a, b = _hit("a"), _hit("b")

    result = _rerank("completely irrelevant prose", [a, b])

    assert result.ok is False
    assert result.hits == [a, b]


def test_empty_reply_falls_back_to_input_order():
    a, b = _hit("a"), _hit("b")

    result = _rerank("", [a, b])

    assert result.ok is False
    assert result.hits == [a, b]


def test_think_tags_are_stripped_before_parsing():
    a, b = _hit("a"), _hit("b")
    reply = '<think>I should weigh {"scores": [1]}</think>{"scores": [2, 9]}'

    result = _rerank(reply, [a, b])

    assert result.ok is True
    assert result.hits == [b, a]


def test_unclosed_think_tag_falls_back_to_input_order():
    a, b = _hit("a"), _hit("b")

    result = _rerank('<think>reasoning about {"scores": [1, 2]} forever', [a, b])

    assert result.ok is False
    assert result.hits == [a, b]


def test_code_fences_are_tolerated():
    a, b = _hit("a"), _hit("b")

    result = _rerank('```json\n{"scores": [2, 9]}\n```', [a, b])

    assert result.ok is True
    assert result.hits == [b, a]


def test_bare_json_array_is_accepted():
    a, b = _hit("a"), _hit("b")

    result = _rerank("[2, 9]", [a, b])

    assert result.ok is True
    assert result.hits == [b, a]


def test_plain_numbers_fallback_is_positional():
    a, b = _hit("a"), _hit("b")

    result = _rerank("2\n9", [a, b])

    assert result.ok is True
    assert result.hits == [b, a]

    result = _rerank("9, 2", [a, b])

    assert result.hits == [a, b]


def test_scores_clamped_to_zero_ten():
    a, b = _hit("a"), _hit("b")

    result = _rerank('{"scores": [15, -3]}', [a, b])

    assert result.ok is True
    assert result.hits == [a, b]


def test_non_finite_score_counts_zero():
    a, b = _hit("a"), _hit("b")

    result = _rerank('{"scores": [NaN, 2]}', [a, b])

    assert result.ok is True
    assert result.hits == [b, a]


def test_top_k_truncates_after_rerank():
    hits = [_hit(cid) for cid in "abcd"]

    result = _rerank('{"scores": [1, 2, 9, 8]}', hits, top_k=2)

    assert [hit.chunk_id for hit in result.hits] == ["c", "d"]


def test_fallback_also_truncates_to_top_k():
    hits = [_hit(cid) for cid in "abcd"]

    result = _rerank("garbage", hits, top_k=2)

    assert [hit.chunk_id for hit in result.hits] == ["a", "b"]


def test_empty_hits_passthrough_without_llm_call():
    llm = FakeLLM(reply='{"scores": [1]}')

    result = Reranker(llm).rerank("hybrid search", [], top_k=5)

    assert result.ok is False
    assert result.hits == []
    assert llm.calls == []


def test_llm_error_falls_back_without_raising():
    llm = FakeLLM(error=LLMError("ollama timeout"))

    result = Reranker(llm).rerank("hybrid search", [_hit("a"), _hit("b")], top_k=5)

    assert result.ok is False
    assert [hit.chunk_id for hit in result.hits] == ["a", "b"]


def test_unexpected_exception_falls_back_without_raising():
    reranker = Reranker(ExplodingLLM(RuntimeError("kaboom")))

    result = reranker.rerank("hybrid search", [_hit("a"), _hit("b")], top_k=5)

    assert result.ok is False
    assert [hit.chunk_id for hit in result.hits] == ["a", "b"]


def test_rrf_scores_are_untouched():
    a, b = _hit("a", score=0.42), _hit("b", score=0.17)

    result = _rerank('{"scores": [1, 9]}', [a, b])

    assert result.hits == [b, a]
    assert result.hits[0].score == 0.17
    assert result.hits[1].score == 0.42


def test_close_delegates_to_llm_close():
    llm = CloseableLLM()
    Reranker(llm).close()
    assert llm.close_calls == 1


def test_close_without_llm_close_is_noop():
    Reranker(FakeLLM()).close()


def test_close_is_repeatable():
    llm = CloseableLLM()
    reranker = Reranker(llm)
    reranker.close()
    reranker.close()
    assert llm.close_calls == 2

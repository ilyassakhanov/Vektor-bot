"""Tests for QueryExpander — FakeLLM only, no network, no Ollama."""

from __future__ import annotations

import json

from llm.base import LLM, ChatResponse, LLMError, LLMResponse, Message, ToolSpec
from retrieval.expansion import ExpandedQuery, QueryExpander
from tests.fakes import FakeLLM


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


def _expand(reply: str, query: str = "hybrid search") -> ExpandedQuery:
    llm = FakeLLM(reply=reply)
    return QueryExpander(llm).expand(query)


def test_valid_json_with_both_keys():
    result = _expand(
        '{"keywords": ["vector search", "rrf fusion"], "queries": ["how does rrf fusion work"]}'
    )
    assert result.original == "hybrid search"
    assert result.keywords == ("vector search", "rrf fusion")
    assert result.alt_queries == ("how does rrf fusion work",)
    assert result.used_expansion is True


def test_json_with_only_keywords():
    result = _expand('{"keywords": ["embeddings", "bm25"]}')
    assert result.keywords == ("embeddings", "bm25")
    assert result.alt_queries == ()
    assert result.used_expansion is True


def test_json_embedded_in_prose_is_extracted():
    result = _expand(
        'Sure! Here you go:\n{"keywords": ["knowledge base"], "queries": ["local docs search"]}\nHope that helps!'
    )
    assert result.keywords == ("knowledge base",)
    assert result.alt_queries == ("local docs search",)
    assert result.used_expansion is True


def test_comma_separated_fallback():
    result = _expand("vector search, semantic retrieval, embeddings")
    assert result.keywords == ("vector search", "semantic retrieval", "embeddings")
    assert result.alt_queries == ()
    assert result.used_expansion is True


def test_malformed_json_without_braces_falls_back_to_tokens():
    result = _expand("term one\nterm two, term three")
    assert result.keywords == ("term one", "term two", "term three")
    assert result.used_expansion is True


def test_truncated_json_with_braces_falls_back_to_original_query():
    result = _expand('{"keywords": ["foo"')
    assert result == ExpandedQuery(
        original="hybrid search", keywords=(), alt_queries=(), used_expansion=False
    )


def test_unclosed_json_object_falls_back_to_original_query():
    result = _expand('{"keywords": ["a", "b"')
    assert result.used_expansion is False
    assert result.keywords == ()
    assert result.alt_queries == ()


def test_json_shaped_garbage_falls_back_to_original_query():
    result = _expand('{"keywords": ["ok"} and then }')
    assert result.used_expansion is False
    assert result.keywords == ()


def test_json_with_non_string_and_non_list_values_is_defensive():
    result = _expand('{"keywords": ["real", 5, null], "queries": "not-a-list"}')
    assert result.keywords == ("real",)
    assert result.alt_queries == ()
    assert result.used_expansion is True


def test_empty_reply_falls_back():
    result = _expand("")
    assert result == ExpandedQuery(
        original="hybrid search", keywords=(), alt_queries=(), used_expansion=False
    )


def test_whitespace_only_reply_falls_back():
    result = _expand("   \n\t  ")
    assert result.used_expansion is False
    assert result.keywords == ()
    assert result.alt_queries == ()


def test_llm_error_falls_back_without_raising():
    llm = FakeLLM(error=LLMError("ollama down"))
    result = QueryExpander(llm).expand("hybrid search")
    assert result.used_expansion is False
    assert result.keywords == ()
    assert result.alt_queries == ()
    assert result.original == "hybrid search"


def test_unexpected_exception_falls_back_without_raising():
    expander = QueryExpander(ExplodingLLM(RuntimeError("kaboom")))
    result = expander.expand("hybrid search")
    assert result.used_expansion is False
    assert result.keywords == ()
    assert result.alt_queries == ()


def test_keywords_capped_at_eight():
    reply = json.dumps({"keywords": [f"k{i}" for i in range(12)], "queries": []})
    result = _expand(reply)
    assert result.keywords == tuple(f"k{i}" for i in range(8))
    assert result.used_expansion is True


def test_alt_queries_capped_at_three():
    result = _expand('{"keywords": [], "queries": ["q1", "q2", "q3", "q4", "q5"]}')
    assert result.alt_queries == ("q1", "q2", "q3")


def test_original_query_excluded_from_keywords_and_queries():
    result = _expand(
        '{"keywords": ["hybrid search", "rrf"], "queries": ["HYBRID SEARCH", "bm25"]}',
        query="hybrid search",
    )
    assert result.keywords == ("rrf",)
    assert result.alt_queries == ("bm25",)


def test_case_insensitive_dedup_keeps_first_casing():
    result = _expand('{"keywords": ["Vector", "vector", "VECTOR", "embedding"]}')
    assert result.keywords == ("Vector", "embedding")


def test_prompt_contains_original_query():
    llm = FakeLLM(reply='{"keywords": ["x"]}')
    QueryExpander(llm).expand("cosine similarity vs dot product")
    assert len(llm.calls) == 1
    assert "cosine similarity vs dot product" in llm.calls[0]

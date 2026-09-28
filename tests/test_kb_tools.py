"""Tests for the kb tools + bot wiring — fakes only, no network, no Ollama.

Covers: ingest→search roundtrip with a FakeEmbedder + tmp SQLite DB,
re-ingest deduplication, fact-sheet formatting (sources tag, no raw scores,
truncation cap), tool error mapping, KB_ENABLED=0 regression (exact pre-kb
tool set), KB_ENABLED default-on registration, an agent end-to-end round
trip through kb_search, and the bot composition roots (build_kb_stack /
build_retriever) driven by environment variables.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.agent import Agent
from bot import build_kb_stack, build_retriever, build_tool_registry
from llm.base import ChatResponse
from retrieval.embeddings import Embedder, EmbeddingError
from retrieval.hybrid import HybridRetriever
from retrieval.rrf import ChunkHit
from retrieval.store import ChunkStore
from retrieval.vector_index import VectorIndex
from tests.fakes import FakeMcpClient, ScriptedLLM, make_tool_call
from tools.base import ToolError
from tools.kb import KbIngestTool, KbSearchTool, StoreFtsAdapter, VectorIndexAdapter
from tools.registry import ToolRegistry

_CHUNK_SIZE = 60
_CHUNK_OVERLAP = 10
_KEYWORDS = ("needle", "haystack", "alpha", "beta", "gamma", "delta")


class FakeEmbedder(Embedder):
    """Deterministic keyword-count embedder; optionally raises on every call."""

    def __init__(self, error: EmbeddingError | None = None) -> None:
        self.calls: list[list[str]] = []
        self._error = error

    @staticmethod
    def vector_for(text: str) -> list[float]:
        lowered = text.lower()
        return [float(lowered.count(keyword)) for keyword in _KEYWORDS] + [1.0]

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._error is not None:
            raise self._error
        return [self.vector_for(text) for text in texts]


def _para(word: str, repeats: int = 15) -> str:
    return " ".join([word] * repeats)


_TEXT = " ".join(
    [
        _para("alpha"),
        _para("needle"),
        _para("beta"),
        _para("gamma"),
    ]
)


def _make_kb(
    tmp_path: Path,
    embedder: Embedder | None = None,
    *,
    fts: bool = True,
    restart: bool = False,
) -> tuple[KbIngestTool, KbSearchTool, ChunkStore]:
    """Wire the kb tools over a tmp SQLite DB with fakes (composition mirror).

    ``restart=True`` simulates a fresh process over an existing DB file: a
    new store/index/adapter set with an EMPTY metadata cache, the vector
    index seeded from the persisted vectors.
    """
    store = ChunkStore(tmp_path / "kb.db")
    the_embedder = embedder or FakeEmbedder()
    vector_index = VectorIndex(dict(store.all_vectors()) if restart else {})
    metadata: dict[str, ChunkHit] = {}
    retriever = HybridRetriever(
        embedder=the_embedder,
        vector=VectorIndexAdapter(vector_index, metadata),
        fts=StoreFtsAdapter(store) if fts else None,
        top_k=5,
    )
    ingest = KbIngestTool(
        store=store,
        embedder=the_embedder,
        vector_index=vector_index,
        metadata=metadata,
        chunk_size=_CHUNK_SIZE,
        chunk_overlap=_CHUNK_OVERLAP,
    )
    search = KbSearchTool(retriever=retriever, store=store)
    return ingest, search, store


# --- Ingest → search roundtrip -------------------------------------------------


def test_ingest_search_roundtrip(tmp_path: Path) -> None:
    ingest, search, _store = _make_kb(tmp_path)
    summary = ingest.execute(text=_TEXT, title="Corpus")
    assert summary.startswith("Ingested ")
    assert "chunks" in summary
    assert "doc " in summary
    assert "Corpus" in summary

    result = search.execute(query="needle")
    assert "needle" in result
    assert "[fts+vector]" in result
    assert "Corpus" in result
    assert "chunk" in result
    assert "score" not in result.lower()


def test_ingest_embeds_all_chunks_in_one_batch(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    ingest, _search, _store = _make_kb(tmp_path, embedder)
    ingest.execute(text=_TEXT)
    assert len(embedder.calls) == 1
    assert len(embedder.calls[0]) > 1


def test_reingest_same_text_does_not_duplicate(tmp_path: Path) -> None:
    ingest, _search, store = _make_kb(tmp_path)
    ingest.execute(text=_TEXT, title="Corpus")
    count_after_first = store.count()
    assert count_after_first > 1
    ingest.execute(text=_TEXT, title="Corpus")
    assert store.count() == count_after_first


def test_ingest_default_title_from_text(tmp_path: Path) -> None:
    ingest, search, _store = _make_kb(tmp_path)
    summary = ingest.execute(text=_TEXT)
    assert "title '" in summary
    assert "alpha" in summary
    result = search.execute(query="needle")
    assert "alpha" in result


# --- kb_search edge cases --------------------------------------------------------


def test_search_empty_result_message(tmp_path: Path) -> None:
    _ingest, search, _store = _make_kb(tmp_path)
    assert (
        search.execute(query="nonexistent-term-zzz") == "No matching knowledge found."
    )


def test_fact_sheet_capped_by_truncate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXEC_MAX_OUTPUT_CHARS", "400")
    big_text = " ".join(_para("gamma", 20) for _ in range(30))
    ingest, search, _store = _make_kb(tmp_path)
    ingest.execute(text=big_text, title="Big")
    result = search.execute(query="gamma")
    assert len(result) <= 400
    assert "[truncated" in result


# --- Tool errors / degradation ---------------------------------------------------


def test_ingest_embedding_error_raises_tool_error(tmp_path: Path) -> None:
    ingest, _search, _store = _make_kb(
        tmp_path, FakeEmbedder(error=EmbeddingError("embedding down"))
    )
    with pytest.raises(ToolError):
        ingest.execute(text=_TEXT)


def test_ingest_error_via_registry_returns_error_string(tmp_path: Path) -> None:
    ingest, _search, _store = _make_kb(
        tmp_path, FakeEmbedder(error=EmbeddingError("embedding down"))
    )
    reg = ToolRegistry()
    reg.register(ingest)
    result = reg.execute("kb_ingest", text=_TEXT)
    assert result.startswith("Error:")


def test_search_vector_only_mode_still_answers(tmp_path: Path) -> None:
    ingest, search, _store = _make_kb(tmp_path, fts=False)
    ingest.execute(text=_TEXT, title="Corpus")
    result = search.execute(query="needle")
    assert "needle" in result
    assert "[vector]" in result


def test_search_never_raises_when_embedding_fails(tmp_path: Path) -> None:
    _ingest, search, _store = _make_kb(
        tmp_path, FakeEmbedder(error=EmbeddingError("embedding down"))
    )
    result = search.execute(query="needle")
    assert isinstance(result, str)
    assert "No matching knowledge found." in result


# --- Restart simulation (fresh process, same DB file, empty metadata cache) ------


def test_restart_hybrid_search_returns_full_metadata(tmp_path: Path) -> None:
    process_a_ingest, _search, _store = _make_kb(tmp_path)
    process_a_ingest.execute(text=_TEXT, title="Corpus")

    _ingest, process_b_search, _store2 = _make_kb(tmp_path, restart=True)
    result = process_b_search.execute(query="needle")

    assert "needle" in result
    assert "Corpus" in result
    assert "(content unavailable)" not in result
    assert "untitled" not in result


def test_restart_vector_only_search_returns_full_metadata(tmp_path: Path) -> None:
    process_a_ingest, _search, _store = _make_kb(tmp_path, fts=False)
    process_a_ingest.execute(text=_TEXT, title="Corpus")

    _ingest, process_b_search, _store2 = _make_kb(tmp_path, fts=False, restart=True)
    result = process_b_search.execute(query="needle")

    assert "needle" in result
    assert "Corpus" in result
    assert "(content unavailable)" not in result


# --- KB_ENABLED regression / default-on ------------------------------------------


def test_kb_enabled_zero_exact_pre_kb_tool_set(monkeypatch) -> None:
    monkeypatch.setenv("KB_ENABLED", "0")
    reg = build_tool_registry(FakeMcpClient())
    assert {spec.name for spec in reg.specs()} == {"exec", "get_latest_cve"}
    reg_exec_only = build_tool_registry(None)
    assert {spec.name for spec in reg_exec_only.specs()} == {"exec"}


def test_kb_enabled_zero_kb_tools_absent(monkeypatch) -> None:
    monkeypatch.setenv("KB_ENABLED", "0")
    reg = build_tool_registry(None)
    names = {spec.name for spec in reg.specs()}
    assert "kb_ingest" not in names
    assert "kb_search" not in names


def test_kb_enabled_default_registers_kb_tools(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_EXPANSION_ENABLED", "0")
    reg = build_tool_registry(None)
    names = {spec.name for spec in reg.specs()}
    assert "kb_ingest" in names
    assert "kb_search" in names
    assert "exec" in names


# --- Agent end-to-end -------------------------------------------------------------


def test_agent_end_to_end_kb_search_roundtrip(tmp_path: Path) -> None:
    ingest, search, _store = _make_kb(tmp_path)
    ingest.execute(text=_TEXT, title="Corpus")

    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[make_tool_call("tc1", "kb_search", {"query": "needle"})],
            ),
            ChatResponse(content="The knowledge base mentions needle."),
        ]
    )
    reg = ToolRegistry()
    reg.register(search)
    agent = Agent(llm, reg)

    result = agent.run("search the knowledge base for needle")

    assert result == "The knowledge base mentions needle."
    assert len(llm.chat_calls) == 2
    tool_msgs = [m for m in llm.chat_calls[1][0] if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "tc1"
    assert "needle" in tool_msgs[0].content


# --- bot composition roots --------------------------------------------------------


def test_build_kb_stack_expansion_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "nested" / "dir" / "kb.db"))
    monkeypatch.setenv("KB_EXPANSION_ENABLED", "0")
    stack = build_kb_stack()
    assert stack is not None
    try:
        assert stack.expander is None
        assert isinstance(stack.retriever, HybridRetriever)
        assert stack.fts is not None
        assert (tmp_path / "nested" / "dir").is_dir()
    finally:
        stack.close()


def test_build_retriever_returns_hybrid_retriever(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_EXPANSION_ENABLED", "0")
    retriever = build_retriever()
    try:
        assert isinstance(retriever, HybridRetriever)
    finally:
        fts = getattr(retriever, "_fts", None)
        store = getattr(fts, "_store", None) if fts is not None else None
        if isinstance(store, ChunkStore):
            store.close()


def test_build_kb_stack_fts_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_FTS_ENABLED", "0")
    stack = build_kb_stack()
    assert stack is not None
    try:
        assert stack.fts is None
    finally:
        stack.close()


def test_build_kb_stack_expansion_enabled_builds_expander(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    stack = build_kb_stack()
    assert stack is not None
    try:
        assert stack.expander is not None
    finally:
        stack.close()


def test_build_kb_stack_disabled_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("KB_ENABLED", "0")
    assert build_kb_stack() is None
    assert build_retriever() is None

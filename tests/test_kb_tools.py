"""Tests for the kb tools + bot wiring — fakes only, no network, no Ollama.

Covers: ingest→search roundtrips, re-ingest dedup, dim validation, concurrent
ingests, fact sheets, tool errors, the ``{text, title}``-only execute schema,
KB_ENABLED regressions, agent end-to-end search, and the bot composition roots.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import pytest

from agent.agent import Agent
from bot import build_kb_stack, build_retriever, build_tool_registry
from llm.base import ChatResponse
from retrieval.embeddings import Embedder, EmbeddingError
from retrieval.expansion import QueryExpander
from retrieval.hybrid import HybridRetriever
from retrieval.principal import get_user, reset_user, set_user
from retrieval.store import (
    META_EMBED_DIM,
    META_EMBED_MODEL,
    ChunkStore,
    KBModelError,
)
from tests.fakes import (
    CloseableLLM,
    FakeLLM,
    FakeMcpClient,
    ScriptedLLM,
    make_tool_call,
)
from tools.base import ToolError
from tools.kb import (
    KbIngestTool,
    KbSearchTool,
    StoreFtsAdapter,
    StoreVecAdapter,
)
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
) -> tuple[KbIngestTool, KbSearchTool, ChunkStore]:
    """Wire the kb tools over a tmp SQLite DB with fakes (composition mirror)."""
    store = ChunkStore(tmp_path / "kb.db")
    the_embedder = embedder or FakeEmbedder()
    retriever = HybridRetriever(
        embedder=the_embedder,
        vector=StoreVecAdapter(store),
        fts=StoreFtsAdapter(store) if fts else None,
        top_k=5,
    )
    ingest = KbIngestTool(
        store=store,
        embedder=the_embedder,
        chunk_size=_CHUNK_SIZE,
        chunk_overlap=_CHUNK_OVERLAP,
    )
    search = KbSearchTool(retriever=retriever)
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


def test_reingest_with_fewer_chunks_removes_stale_content(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path / "kb.db")
    embedder = FakeEmbedder()

    def make_ingest(size: int, overlap: int) -> KbIngestTool:
        return KbIngestTool(
            store=store,
            embedder=embedder,
            chunk_size=size,
            chunk_overlap=overlap,
        )

    big_ingest = make_ingest(_CHUNK_SIZE, _CHUNK_OVERLAP)
    whole_ingest = make_ingest(10_000, 0)
    big_ingest.execute(text=_TEXT, title="Corpus")
    assert store.count() > 1

    whole_ingest.execute(text=_TEXT, title="Corpus")

    assert store.count() == 1
    assert [hit.idx for hit in store.search_fts("0", ["needle"], limit=10)] == [0]
    search = KbSearchTool(
        retriever=HybridRetriever(
            embedder=embedder,
            vector=StoreVecAdapter(store),
            fts=StoreFtsAdapter(store),
            top_k=5,
        ),
    )
    result = search.execute(query="needle")
    assert "needle" in result
    assert "(chunk 0)" in result


def test_concurrent_ingests_over_one_store_stay_consistent(tmp_path: Path) -> None:
    """Two tools sharing one store: both persist, everything is searchable."""
    store = ChunkStore(tmp_path / "kb.db")
    embedder = FakeEmbedder()

    def make_tool() -> KbIngestTool:
        return KbIngestTool(
            store=store,
            embedder=embedder,
            chunk_size=_CHUNK_SIZE,
            chunk_overlap=_CHUNK_OVERLAP,
        )

    errors: list[BaseException] = []

    def run(tool: KbIngestTool, **kwargs: str) -> None:
        try:
            tool.execute(**kwargs)
        except BaseException as exc:  # noqa: BLE001 — surfaced by the assert below
            errors.append(exc)

    threads = [
        threading.Thread(
            target=run, args=(make_tool(),), kwargs={"text": _TEXT, "title": "A"}
        ),
        threading.Thread(
            target=run,
            args=(make_tool(),),
            kwargs={"text": _para("zeta", 20), "title": "B"},
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert {
        hit.title for hit in store.search_fts("0", ["alpha", "zeta"], limit=10)
    } == {
        "A",
        "B",
    }


def test_ingest_persists_embed_dim_meta(tmp_path: Path) -> None:
    ingest, _search, store = _make_kb(tmp_path)
    ingest.execute(text=_TEXT, title="Corpus")
    assert store.get_meta(META_EMBED_DIM) == str(len(FakeEmbedder.vector_for("x")))


def test_ingest_dim_mismatch_raises_tool_error_and_writes_nothing(
    tmp_path: Path,
) -> None:
    store = ChunkStore(tmp_path / "kb.db")
    store.set_meta(META_EMBED_DIM, "3")
    assert len(FakeEmbedder.vector_for("x")) != 3
    ingest = KbIngestTool(
        store=store,
        embedder=FakeEmbedder(),
        chunk_size=_CHUNK_SIZE,
        chunk_overlap=_CHUNK_OVERLAP,
    )
    with pytest.raises(ToolError, match="dimension"):
        ingest.execute(text=_TEXT)
    assert store.count() == 0


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


# --- Fact sheet: page attribution (WS-4) -------------------------------------------


def test_execute_schema_is_exactly_text_and_title(tmp_path: Path) -> None:
    ingest, _search, _store = _make_kb(tmp_path)
    assert set(ingest.parameters["properties"]) == {"text", "title"}


def test_execute_rejects_non_schema_kwargs(tmp_path: Path) -> None:
    """The LLM-facing execute honors ONLY {text, title}: a hallucinating LLM
    passing filename/file_type/pages gets a ToolError and nothing is stored —
    page/file metadata can never be forged through the tool boundary."""
    ingest, _search, store = _make_kb(tmp_path)
    for extra in (
        {"pages": [(1, "forged page text")]},
        {"filename": "forged.txt"},
        {"file_type": "pdf"},
        {"filename": "f.txt", "file_type": "txt", "pages": [(9, "x")]},
    ):
        with pytest.raises(ToolError, match="Unsupported argument"):
            ingest.execute(text="hello", title="T", **extra)
    assert store.count() == 0
    assert store.list_documents("0") == []


def test_ingest_document_is_the_bot_level_entry(tmp_path: Path) -> None:
    """ingest_document accepts the metadata the bot passes (filename,
    file_type, pages) and it all lands in the store."""
    ingest, search, store = _make_kb(tmp_path)
    ingest.ingest_document(
        text=_para("needle", 8),
        title="Paged",
        filename="notes.txt",
        file_type="txt",
        pages=[(1, _para("needle", 8))],
    )
    hits = store.search_fts("0", ["needle"], limit=5)
    assert [hit.page for hit in hits] == [1]
    assert hits[0].title == "notes.txt"
    assert "(chunk 0, page 1)" in search.execute(query="needle")


def test_ingest_document_with_pages_persists_page_numbers(tmp_path: Path) -> None:
    ingest, _search, store = _make_kb(tmp_path)
    ingest.ingest_document(
        text="alpha needle",
        title="Paged",
        filename="book.pdf",
        file_type="pdf",
        pages=[(1, _para("alpha", 8)), (2, _para("needle", 8))],
    )
    assert [hit.page for hit in store.search_fts("0", ["needle"], limit=5)] == [2]
    vec_hits = store.search_vec("0", [FakeEmbedder.vector_for("needle")], limit=5)
    assert vec_hits[0].page == 2


def test_ingest_without_pages_persists_null_page(tmp_path: Path) -> None:
    ingest, _search, store = _make_kb(tmp_path)
    ingest.execute(text=_para("needle", 8), title="Plain")
    assert [hit.page for hit in store.search_fts("0", ["needle"], limit=5)] == [None]


def test_fact_sheet_shows_page_when_present(tmp_path: Path) -> None:
    ingest, search, _store = _make_kb(tmp_path)
    ingest.ingest_document(
        text="alpha needle",
        title="Paged",
        filename="book.pdf",
        file_type="pdf",
        pages=[(1, _para("alpha", 8)), (2, _para("needle", 8))],
    )
    result = search.execute(query="needle")
    line = next(line for line in result.splitlines() if "(chunk " in line)
    assert "(chunk 1, page 2)" in line


def test_fact_sheet_omits_page_when_absent(tmp_path: Path) -> None:
    ingest, search, _store = _make_kb(tmp_path)
    ingest.execute(text=_para("needle", 8), title="Plain")
    result = search.execute(query="needle")
    assert "(chunk 0)" in result
    assert ", page" not in result


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


# --- Restart simulation (fresh process, same DB file) -----------------------------


def test_restart_hybrid_search_returns_full_metadata(tmp_path: Path) -> None:
    process_a_ingest, _search, store = _make_kb(tmp_path)
    process_a_ingest.execute(text=_TEXT, title="Corpus")
    store.close()

    _ingest, process_b_search, store2 = _make_kb(tmp_path)
    try:
        result = process_b_search.execute(query="needle")
        assert "needle" in result
        assert "Corpus" in result
        assert "(content unavailable)" not in result
        assert "untitled" not in result
    finally:
        store2.close()


def test_restart_vector_only_search_returns_full_metadata(tmp_path: Path) -> None:
    process_a_ingest, _search, store = _make_kb(tmp_path, fts=False)
    process_a_ingest.execute(text=_TEXT, title="Corpus")
    store.close()

    _ingest, process_b_search, store2 = _make_kb(tmp_path, fts=False)
    try:
        result = process_b_search.execute(query="needle")
        assert "needle" in result
        assert "Corpus" in result
        assert "(content unavailable)" not in result
    finally:
        store2.close()


# --- Principal contextvar (WS-2) ---------------------------------------------------


def test_tool_schemas_expose_no_user_id(tmp_path: Path) -> None:
    ingest, search, _store = _make_kb(tmp_path)
    for tool in (ingest, search):
        properties = tool.parameters["properties"]
        assert "user_id" not in properties
        assert "user_id" not in tool.parameters.get("required", [])


def test_no_principal_falls_back_to_zero_owner(tmp_path: Path) -> None:
    """Explicit fallback: without a principal everything lands under "0"
    and is only visible to "0" — never silently re-scoped."""
    ingest, search, store = _make_kb(tmp_path)
    ingest.execute(text=_TEXT, title="Corpus")
    assert {h.title for h in store.search_fts("0", ["needle"], limit=5)} == {"Corpus"}
    assert "needle" in search.execute(query="needle")


def test_principal_scopes_ingest_and_search(tmp_path: Path) -> None:
    ingest, search, _store = _make_kb(tmp_path)
    owner = set_user("101")
    try:
        ingest.execute(text=_TEXT, title="Corpus")
    finally:
        reset_user(owner)

    other = set_user("202")
    try:
        assert search.execute(query="needle") == "No matching knowledge found."
    finally:
        reset_user(other)

    owner = set_user("101")
    try:
        result = search.execute(query="needle")
        assert "needle" in result
        assert "Corpus" in result
    finally:
        reset_user(owner)


def test_ingest_document_uses_filename_file_type_and_utc_created_at(
    tmp_path: Path,
) -> None:
    ingest, _search, _store = _make_kb(tmp_path)
    ingest.ingest_document(
        text=_TEXT, title="Corpus", filename="notes.txt", file_type="txt"
    )
    conn = sqlite3.connect(tmp_path / "kb.db")
    try:
        row = conn.execute(
            "SELECT user_id, filename, file_type, created_at FROM documents"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "0"
    assert row[1] == "notes.txt"
    assert row[2] == "txt"
    parsed = datetime.fromisoformat(row[3])
    assert parsed.tzinfo is not None


def test_get_user_default_is_zero_without_principal() -> None:
    assert get_user() == "0"


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


def test_auto_build_kb_false_never_retries_degraded_kb(
    monkeypatch, tmp_path: Path
) -> None:
    """main() after a degraded KB startup passes kb=None WITHOUT auto-build.

    A None kb there means "disabled/degraded at startup" — the registry
    must not silently rebuild the stack (it would back agent kb tools with
    an untracked, unclosed stack while document uploads stay disabled).
    """
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_EXPANSION_ENABLED", "0")

    reg = build_tool_registry(None, kb=None, auto_build_kb=False)
    names = {spec.name for spec in reg.specs()}
    assert "kb_ingest" not in names
    assert "kb_search" not in names
    assert "exec" in names

    # No stack was built behind the scenes — the DB file was never created.
    assert not (tmp_path / "kb.db").exists()


def test_auto_build_kb_default_still_builds(monkeypatch, tmp_path: Path) -> None:
    """The default keeps auto-building — plain build_tool_registry(None) works."""
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_EXPANSION_ENABLED", "0")
    reg = build_tool_registry(None, kb=None)
    names = {spec.name for spec in reg.specs()}
    assert {"kb_ingest", "kb_search"} <= names


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


def test_build_kb_stack_close_closes_expansion_llm(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    stack = build_kb_stack()
    assert stack is not None
    assert stack.expander is not None
    llm = CloseableLLM()
    stack.expander = QueryExpander(llm)
    stack.close()
    assert llm.close_calls == 1


def test_build_kb_stack_close_tolerates_llm_without_close(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    stack = build_kb_stack()
    assert stack is not None
    assert stack.expander is not None
    stack.expander = QueryExpander(FakeLLM(reply="ok"))
    stack.close()


def test_build_kb_stack_disabled_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("KB_ENABLED", "0")
    assert build_kb_stack() is None
    assert build_retriever() is None


# --- persisted embedding model ----------------------------------------------------


def test_build_kb_stack_persists_embed_model(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_EXPANSION_ENABLED", "0")
    monkeypatch.setenv("OLLAMA_EMBED_MODEL", "persisted-embedder")
    stack = build_kb_stack()
    assert stack is not None
    try:
        assert stack.store.get_meta(META_EMBED_MODEL) == "persisted-embedder"
    finally:
        stack.close()


def test_build_kb_stack_model_mismatch_raises(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_EXPANSION_ENABLED", "0")
    store = ChunkStore(tmp_path / "kb.db")
    store.set_meta(META_EMBED_MODEL, "old-model")
    store.close()

    with pytest.raises(KBModelError, match="OLLAMA_EMBED_MODEL"):
        build_kb_stack()


def test_build_kb_stack_model_match_reopens(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_EXPANSION_ENABLED", "0")
    monkeypatch.setenv("OLLAMA_EMBED_MODEL", "same-model")
    stack = build_kb_stack()
    assert stack is not None
    stack.close()

    stack2 = build_kb_stack()
    assert stack2 is not None
    try:
        assert stack2.store.get_meta(META_EMBED_MODEL) == "same-model"
    finally:
        stack2.close()


def test_build_tool_registry_model_mismatch_propagates(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_EXPANSION_ENABLED", "0")
    store = ChunkStore(tmp_path / "kb.db")
    store.set_meta(META_EMBED_MODEL, "old-model")
    store.close()

    with pytest.raises(KBModelError):
        build_tool_registry(None)


def test_kb_stack_exports_store_backed_adapters(monkeypatch, tmp_path: Path) -> None:
    """KbStack wires StoreVecAdapter/StoreFtsAdapter over the shared store."""
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_EXPANSION_ENABLED", "0")
    stack = build_kb_stack()
    assert stack is not None
    try:
        assert isinstance(stack.vector, StoreVecAdapter)
        assert stack.fts is not None
        assert not hasattr(stack, "vector_index")
        assert not hasattr(stack, "metadata")
        assert not hasattr(stack, "ingest_lock")
    finally:
        stack.close()

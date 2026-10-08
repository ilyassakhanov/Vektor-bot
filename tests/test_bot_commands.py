"""Tests for the /documents and /delete bot commands.

Offline: a real ChunkStore over tmp SQLite (fixtures ingested via
``KbIngestTool.ingest_document``); handlers invoked with SimpleNamespace
messages; create_bot routing asserted on the TeleBot handler registry.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.agent import Agent
from agent.conversation import ConversationManager
from bot import (
    _AUTH_DENIED_REPLY,
    _DELETE_USAGE_REPLY,
    _DOCUMENTS_EMPTY_REPLY,
    _DOCUMENTS_HEADER,
    _KB_DISABLED_REPLY,
    create_bot,
    handle_delete_command,
    handle_documents_command,
)
from retrieval.config import RetrievalConfig
from retrieval.embeddings import Embedder
from retrieval.hybrid import HybridRetriever
from retrieval.principal import reset_user, set_user
from retrieval.store import ChunkStore
from tests.fakes import ScriptedLLM
from tools.kb import (
    KbIngestTool,
    KbSearchTool,
    KbStack,
    StoreFtsAdapter,
    StoreVecAdapter,
)
from tools.registry import ToolRegistry

_KEYWORDS = ("needle", "haystack", "alpha", "beta")
_CHUNK_SIZE = 60
_CHUNK_OVERLAP = 10


# --- Fakes and stack helpers (self-contained, no cross-test imports) -----------


class FakeEmbedder(Embedder):
    """Deterministic keyword-count embedder (mirrors test_bot_documents)."""

    @staticmethod
    def vector_for(text: str) -> list[float]:
        lowered = text.lower()
        return [float(lowered.count(keyword)) for keyword in _KEYWORDS] + [1.0]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.vector_for(text) for text in texts]


def _para(word: str, count: int) -> str:
    return " ".join([word] * count)


def _make_kb(tmp_path: Path) -> KbStack:
    """Wire a real kb stack over a tmp SQLite DB (mirrors build_kb_stack)."""
    cfg = RetrievalConfig(
        kb_enabled=True,
        kb_db_path=tmp_path / "kb.db",
        kb_chunk_size=_CHUNK_SIZE,
        kb_chunk_overlap=_CHUNK_OVERLAP,
        kb_expansion_enabled=False,
    )
    store = ChunkStore(cfg.kb_db_path)
    embedder = FakeEmbedder()
    vector = StoreVecAdapter(store)
    fts = StoreFtsAdapter(store)
    retriever = HybridRetriever(
        embedder=embedder,
        vector=vector,
        fts=fts,
        vector_limit=cfg.kb_vector_limit,
        fts_limit=cfg.kb_fts_limit,
        top_k=cfg.kb_top_k,
        rrf_k=cfg.kb_rrf_k,
    )
    return KbStack(
        cfg=cfg,
        store=store,
        embedder=embedder,
        vector=vector,
        retriever=retriever,
        fts=fts,
    )


def _ingest(stack: KbStack, filename: str, text: str, user_id: str) -> None:
    token = set_user(user_id)
    try:
        tool = KbIngestTool(
            store=stack.store,
            embedder=stack.embedder,
            chunk_size=stack.cfg.kb_chunk_size,
            chunk_overlap=stack.cfg.kb_chunk_overlap,
        )
        tool.ingest_document(
            text=text,
            title=filename,
            filename=filename,
            file_type=Path(filename).suffix.lstrip(".").lower(),
        )
    finally:
        reset_user(token)


def _command(
    text: str, user_id: int = 101, username: str | None = "tester"
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=user_id, username=username, first_name="T"),
        text=text,
    )


def _capture(replies: list[str]) -> Callable[[SimpleNamespace, str], None]:
    def reply(_message: SimpleNamespace, text: str) -> None:
        replies.append(text)

    return reply


def _make_conv() -> ConversationManager:
    return ConversationManager(Agent(ScriptedLLM([]), ToolRegistry()))


# --- /documents -------------------------------------------------------------------


def test_documents_lists_documents_with_date_and_chunks(tmp_path: Path) -> None:
    stack = _make_kb(tmp_path)
    _ingest(stack, "notes.txt", _para("alpha", 30), "101")
    replies: list[str] = []
    handle_documents_command(
        _command("/documents", user_id=101), _capture(replies), None, stack.store
    )
    assert len(replies) == 1
    assert replies[0].startswith(_DOCUMENTS_HEADER)
    row = next(
        r for r in stack.store.list_documents("101") if r.filename == "notes.txt"
    )
    assert "1. notes.txt" in replies[0]
    assert row.created_at[:10] in replies[0]
    assert f"{row.chunk_count} chunks" in replies[0]


def test_documents_lists_multiple_documents(tmp_path: Path) -> None:
    stack = _make_kb(tmp_path)
    _ingest(stack, "notes.txt", _para("alpha", 30), "101")
    _ingest(stack, "guide.md", _para("beta", 30), "101")
    replies: list[str] = []
    handle_documents_command(
        _command("/documents", user_id=101), _capture(replies), None, stack.store
    )
    entries = [line.split(". ", 1)[1] for line in replies[0].splitlines()[1:]]
    assert len(entries) == 2
    assert {entry.split(" — ")[0] for entry in entries} == {"notes.txt", "guide.md"}


def test_documents_scoped_to_caller(tmp_path: Path) -> None:
    stack = _make_kb(tmp_path)
    _ingest(stack, "secret.txt", _para("alpha", 30), "101")
    replies_b: list[str] = []
    handle_documents_command(
        _command("/documents", user_id=202), _capture(replies_b), None, stack.store
    )
    assert replies_b == [_DOCUMENTS_EMPTY_REPLY]
    replies_a: list[str] = []
    handle_documents_command(
        _command("/documents", user_id=101), _capture(replies_a), None, stack.store
    )
    assert "secret.txt" in replies_a[0]


def test_documents_empty_state(tmp_path: Path) -> None:
    stack = _make_kb(tmp_path)
    replies: list[str] = []
    handle_documents_command(
        _command("/documents", user_id=101), _capture(replies), None, stack.store
    )
    assert replies == [_DOCUMENTS_EMPTY_REPLY]


def test_documents_unauthorized_never_touches_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = _make_kb(tmp_path)
    calls: list[str] = []

    def record(user_id: str) -> list[object]:
        calls.append(user_id)
        return []

    monkeypatch.setattr(stack.store, "list_documents", record)
    replies: list[str] = []
    handle_documents_command(
        _command("/documents", username="intruder"),
        _capture(replies),
        frozenset({"tester"}),
        stack.store,
    )
    assert replies == [_AUTH_DENIED_REPLY]
    assert calls == []


def test_documents_user_without_username_denied(tmp_path: Path) -> None:
    stack = _make_kb(tmp_path)
    replies: list[str] = []
    handle_documents_command(
        _command("/documents", username=None),
        _capture(replies),
        frozenset({"tester"}),
        stack.store,
    )
    assert replies == [_AUTH_DENIED_REPLY]


def test_documents_kb_disabled_replies_not_enabled() -> None:
    replies: list[str] = []
    handle_documents_command(_command("/documents"), _capture(replies), None, None)
    assert replies == [_KB_DISABLED_REPLY]


# --- /delete ------------------------------------------------------------------------


def test_delete_removes_document_and_search_cascade(tmp_path: Path) -> None:
    stack = _make_kb(tmp_path)
    _ingest(stack, "notes.txt", _para("needle", 30), "101")
    replies: list[str] = []
    handle_delete_command(
        _command("/delete notes.txt", user_id=101),
        _capture(replies),
        None,
        stack.store,
    )
    assert len(replies) == 1
    assert "notes.txt" in replies[0]
    assert stack.store.list_documents("101") == []
    token = set_user("101")
    try:
        result = KbSearchTool(retriever=stack.retriever).execute(query="needle")
    finally:
        reset_user(token)
    assert result == "No matching knowledge found."


def test_delete_unknown_document_replies_not_found(tmp_path: Path) -> None:
    stack = _make_kb(tmp_path)
    replies: list[str] = []
    handle_delete_command(
        _command("/delete ghost.txt", user_id=101),
        _capture(replies),
        None,
        stack.store,
    )
    assert len(replies) == 1
    assert "ghost.txt" in replies[0]


def test_delete_document_of_other_user_stays(tmp_path: Path) -> None:
    stack = _make_kb(tmp_path)
    _ingest(stack, "notes.txt", _para("needle", 30), "101")
    replies: list[str] = []
    handle_delete_command(
        _command("/delete notes.txt", user_id=202),
        _capture(replies),
        None,
        stack.store,
    )
    assert len(replies) == 1
    assert "notes.txt" in replies[0]
    assert len(stack.store.list_documents("101")) == 1


def test_delete_without_filename_shows_usage(tmp_path: Path) -> None:
    stack = _make_kb(tmp_path)
    replies: list[str] = []
    handle_delete_command(
        _command("/delete", user_id=101), _capture(replies), None, stack.store
    )
    handle_delete_command(
        _command("/delete   ", user_id=101), _capture(replies), None, stack.store
    )
    assert replies == [_DELETE_USAGE_REPLY, _DELETE_USAGE_REPLY]


def test_delete_filename_with_spaces(tmp_path: Path) -> None:
    stack = _make_kb(tmp_path)
    _ingest(stack, "my report.txt", _para("needle", 30), "101")
    replies: list[str] = []
    handle_delete_command(
        _command("/delete my report.txt", user_id=101),
        _capture(replies),
        None,
        stack.store,
    )
    assert stack.store.list_documents("101") == []


def test_delete_unauthorized_never_touches_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = _make_kb(tmp_path)
    calls: list[tuple[str, str]] = []

    def record(user_id: str, filename: str) -> bool:
        calls.append((user_id, filename))
        return True

    monkeypatch.setattr(stack.store, "delete_document", record)
    replies: list[str] = []
    handle_delete_command(
        _command("/delete notes.txt", username="intruder"),
        _capture(replies),
        frozenset({"tester"}),
        stack.store,
    )
    assert replies == [_AUTH_DENIED_REPLY]
    assert calls == []


# --- create_bot routing ---------------------------------------------------------------


def test_create_bot_registers_documents_and_delete_commands(tmp_path: Path) -> None:
    stack = _make_kb(tmp_path)
    tb = create_bot(_make_conv(), frozenset({"tester"}), kb=stack)
    commands = [
        command
        for handler in tb.message_handlers
        for command in handler["filters"].get("commands") or []
    ]
    assert "documents" in commands
    assert "delete" in commands


def test_command_handlers_run_before_catch_all(tmp_path: Path) -> None:
    """The catch-all text handler is registered LAST — registered before it
    would swallow /documents and /delete as plain text."""
    stack = _make_kb(tmp_path)
    tb = create_bot(_make_conv(), frozenset({"tester"}), kb=stack)
    flags = [
        bool(handler["filters"].get("commands")) for handler in tb.message_handlers
    ]
    assert flags == [True, True, False]


def test_documents_command_wired_with_store_and_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = _make_kb(tmp_path)
    _ingest(stack, "notes.txt", _para("alpha", 30), "101")
    tb = create_bot(_make_conv(), frozenset({"tester"}), kb=stack)
    sent: list[str] = []
    monkeypatch.setattr(tb, "reply_to", lambda _m, text, **_kw: sent.append(text))
    tb.message_handlers[0]["function"](_command("/documents", username="tester"))
    assert any("notes.txt" in text for text in sent)


def test_delete_command_wired_with_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = _make_kb(tmp_path)
    _ingest(stack, "notes.txt", _para("needle", 30), "101")
    tb = create_bot(_make_conv(), frozenset({"tester"}), kb=stack)
    sent: list[str] = []
    monkeypatch.setattr(tb, "reply_to", lambda _m, text, **_kw: sent.append(text))
    tb.message_handlers[1]["function"](_command("/delete notes.txt", username="tester"))
    assert stack.store.list_documents("101") == []
    assert any("notes.txt" in text for text in sent)


def test_commands_kb_none_still_registered() -> None:
    tb = create_bot(_make_conv(), frozenset({"tester"}), kb=None)
    commands = [
        command
        for handler in tb.message_handlers
        for command in handler["filters"].get("commands") or []
    ]
    assert "documents" in commands
    assert "delete" in commands

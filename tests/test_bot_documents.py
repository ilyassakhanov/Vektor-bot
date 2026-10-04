"""Tests for the document-upload flow — bot-level ingest, then every upload
routes an upload notice (+ caption) through the conversation: confirmation
+ agent reply.

Offline: a fake TeleBot records get_file/download_file calls and hands out
canned bytes; the kb stack is real over a tmp SQLite DB with a deterministic
fake embedder; the agent runs against ScriptedLLM/FakeLLM. No network, no
Ollama, no real Telegram.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from telebot.apihelper import ApiTelegramException

import bot as bot_module
from agent.agent import Agent
from agent.conversation import ConversationManager
from bot import build_document_handler, create_bot, handle_message
from llm import LLMError
from llm.base import ChatResponse
from retrieval.config import RetrievalConfig
from retrieval.embeddings import Embedder, EmbeddingError
from retrieval.hybrid import HybridRetriever
from retrieval.rrf import ChunkHit
from retrieval.store import ChunkStore
from retrieval.vector_index import VectorIndex
from tests.fakes import FakeLLM, ScriptedLLM
from tools.kb import KbSearchTool, KbStack, StoreFtsAdapter, VectorIndexAdapter
from tools.registry import ToolRegistry

_FILE_ID = "f1"
_FILE_PATH = "documents/file_1.txt"
_CHUNK_SIZE = 60
_CHUNK_OVERLAP = 10
_KEYWORDS = ("needle", "haystack", "alpha", "beta")
_DOC_BODY = " ".join(["needle"] * 20)
_CONFIRMATION_PREFIX = "Ingested "
_DISABLED_REPLY = "Document uploads are not enabled."
_LLM_ERROR_REPLY = "Sorry, I couldn't generate a response."
_DOC_ERROR_REPLY = "Sorry, I couldn't process that document."


# --- Fakes and stack helpers (self-contained, no cross-test imports) -----------


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


class FakeBot:
    """Fake TeleBot — returns canned bytes or raises; records every call."""

    def __init__(
        self,
        content: bytes,
        error: Exception | None = None,
        download_error: Exception | None = None,
        file_path: str | None = _FILE_PATH,
    ) -> None:
        self.content = content
        self._error = error
        self._download_error = download_error
        self._file_path = file_path
        self.get_file_calls: list[str] = []
        self.download_calls: list[str] = []

    def get_file(self, file_id: str) -> SimpleNamespace:
        self.get_file_calls.append(file_id)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(file_id=file_id, file_path=self._file_path)

    def download_file(self, file_path: str) -> bytes:
        self.download_calls.append(file_path)
        if self._download_error is not None:
            raise self._download_error
        if self._error is not None:
            raise self._error
        return self.content


def _make_kb(tmp_path: Path, embedder: Embedder | None = None) -> KbStack:
    """Wire a real kb stack over a tmp SQLite DB (mirrors build_kb_stack)."""
    cfg = RetrievalConfig(
        kb_enabled=True,
        kb_db_path=tmp_path / "kb.db",
        kb_chunk_size=_CHUNK_SIZE,
        kb_chunk_overlap=_CHUNK_OVERLAP,
        kb_expansion_enabled=False,
    )
    store = ChunkStore(cfg.kb_db_path)
    emb = embedder if embedder is not None else FakeEmbedder()
    vector_index = VectorIndex(dict(store.all_vectors()))
    metadata: dict[str, ChunkHit] = {}
    vector = VectorIndexAdapter(vector_index, metadata)
    fts = StoreFtsAdapter(store)
    retriever = HybridRetriever(
        embedder=emb,
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
        embedder=emb,
        vector_index=vector_index,
        vector=vector,
        retriever=retriever,
        metadata=metadata,
        fts=fts,
    )


def _make_conv(llm) -> ConversationManager:
    return ConversationManager(Agent(llm, ToolRegistry()))


def _make_doc_message(
    file_name: str | None,
    caption: str | None = None,
    username: str | None = "tester",
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=1, username=username, first_name="Tester"),
        document=SimpleNamespace(file_id=_FILE_ID, file_name=file_name),
        caption=caption,
    )


def _make_text_message(
    text: str,
    username: str | None = "tester",
) -> SimpleNamespace:
    """Existing-style fixture WITHOUT a ``document`` attribute."""
    return SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=1, username=username, first_name="Tester"),
        text=text,
    )


def _capture(replies: list[str]) -> Callable[[SimpleNamespace, str], None]:
    def reply(_message: SimpleNamespace, text: str) -> None:
        replies.append(text)

    return reply


def _handle_document(
    bot: FakeBot,
    kb: KbStack,
    conv: ConversationManager,
    message: SimpleNamespace,
    replies: list[str],
) -> None:
    handler = build_document_handler(bot, kb, conv)
    handler(message, _capture(replies))


# --- Document flow ---------------------------------------------------------------


def test_document_downloaded_extracted_ingested_confirmed(tmp_path: Path) -> None:
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    llm = ScriptedLLM([ChatResponse(content="noted")])
    conv = _make_conv(llm)
    replies: list[str] = []
    _handle_document(bot, kb, conv, _make_doc_message("notes.txt"), replies)
    assert bot.get_file_calls == [_FILE_ID]
    assert bot.download_calls == [_FILE_PATH]
    assert len(replies) == 2
    assert replies[0].startswith(_CONFIRMATION_PREFIX)
    assert "notes.txt" in replies[0]
    assert replies[1] == "noted"
    assert len(llm.chat_calls) == 1


def test_ingested_document_findable_via_kb_search(tmp_path: Path) -> None:
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    replies: list[str] = []
    _handle_document(
        bot,
        kb,
        _make_conv(ScriptedLLM([ChatResponse(content="done")])),
        _make_doc_message("notes.txt"),
        replies,
    )
    search = KbSearchTool(retriever=kb.retriever, store=kb.store)
    result = search.execute(query="needle")
    assert "needle" in result
    assert "notes.txt" in result


def test_document_without_name_gets_friendly_error(tmp_path: Path) -> None:
    """A nameless document falls back to the name "document" — no extension,
    so extraction cannot dispatch and the handler replies with the friendly
    document error (spec: ``file_name or "document"``, extract on suffix)."""
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    llm = ScriptedLLM([])
    conv = _make_conv(llm)
    replies: list[str] = []
    _handle_document(bot, kb, conv, _make_doc_message(None), replies)
    assert replies == [_DOC_ERROR_REPLY]
    assert bot.get_file_calls == [_FILE_ID]
    assert bot.download_calls == [_FILE_PATH]
    assert llm.chat_calls == []


# --- Caption routing ---------------------------------------------------------------


def test_caption_routed_to_agent_two_replies_in_order(tmp_path: Path) -> None:
    llm = ScriptedLLM([ChatResponse(content="agent answer")])
    conv = _make_conv(llm)
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    replies: list[str] = []
    caption = "What is in the document?"
    message = _make_doc_message("notes.txt", caption=caption)
    _handle_document(bot, kb, conv, message, replies)
    assert len(replies) == 2
    assert replies[0].startswith(_CONFIRMATION_PREFIX)
    assert replies[1] == "agent answer"
    messages, _tools, _system = llm.chat_calls[0]
    assert messages[-1].role == "user"
    prompt = messages[-1].content
    assert "notes.txt" in prompt
    assert "kb_search" in prompt
    assert prompt.endswith(caption)


def test_no_caption_two_replies_confirmation_then_agent(tmp_path: Path) -> None:
    llm = ScriptedLLM([ChatResponse(content="ack — the document is searchable")])
    conv = _make_conv(llm)
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    replies: list[str] = []
    _handle_document(bot, kb, conv, _make_doc_message("notes.txt"), replies)
    assert len(replies) == 2
    assert replies[0].startswith(_CONFIRMATION_PREFIX)
    assert replies[1] == "ack — the document is searchable"


def test_caption_llm_error_confirmation_then_friendly_reply(tmp_path: Path) -> None:
    llm = FakeLLM(error=LLMError("boom"))
    conv = _make_conv(llm)
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    replies: list[str] = []
    message = _make_doc_message("notes.txt", caption="Summarize this")
    _handle_document(bot, kb, conv, message, replies)
    assert len(replies) == 2
    assert replies[0].startswith(_CONFIRMATION_PREFIX)
    assert replies[1] == _LLM_ERROR_REPLY


# --- KB-disabled and auth-gate routing via handle_message -------------------------


def test_document_without_handler_replies_not_enabled(tmp_path: Path) -> None:
    llm = ScriptedLLM([])
    conv = _make_conv(llm)
    replies: list[str] = []
    handle_message(
        _make_doc_message("notes.txt"),
        conv,
        _capture(replies),
        None,
        None,
    )
    assert replies == [_DISABLED_REPLY]
    assert llm.chat_calls == []


def test_unauthorized_document_never_downloaded(tmp_path: Path) -> None:
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    llm = ScriptedLLM([])
    conv = _make_conv(llm)
    handler = build_document_handler(bot, kb, conv)
    replies: list[str] = []
    message = _make_doc_message("notes.txt", username="intruder")
    handle_message(message, conv, _capture(replies), frozenset({"tester"}), handler)
    assert replies == ["Sorry, you are not allowed to use this bot."]
    assert bot.get_file_calls == []
    assert bot.download_calls == []
    assert llm.chat_calls == []


def test_message_without_document_attribute_text_path_unchanged(tmp_path: Path) -> None:
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    llm = FakeLLM(reply="hello there")
    conv = _make_conv(llm)
    handler = build_document_handler(bot, kb, conv)
    replies: list[str] = []
    handle_message(
        _make_text_message("hi"),
        conv,
        _capture(replies),
        None,
        handler,
    )
    assert replies == ["hello there"]
    assert bot.get_file_calls == []
    assert bot.download_calls == []


# --- Error paths: one friendly reply, never a crash --------------------------------


def test_unknown_extension_friendly_single_error_reply(tmp_path: Path) -> None:
    bot = FakeBot(b"MZ fake binary")
    kb = _make_kb(tmp_path)
    llm = ScriptedLLM([])
    conv = _make_conv(llm)
    replies: list[str] = []
    _handle_document(bot, kb, conv, _make_doc_message("virus.exe"), replies)
    assert replies == [_DOC_ERROR_REPLY]
    assert llm.chat_calls == []


def test_corrupt_pdf_bytes_friendly_single_error_reply(tmp_path: Path) -> None:
    bot = FakeBot(b"%PDF- not really a pdf at all")
    kb = _make_kb(tmp_path)
    llm = ScriptedLLM([])
    conv = _make_conv(llm)
    replies: list[str] = []
    _handle_document(bot, kb, conv, _make_doc_message("broken.pdf"), replies)
    assert replies == [_DOC_ERROR_REPLY]
    assert llm.chat_calls == []


def test_api_error_on_get_file_friendly_reply(tmp_path: Path) -> None:
    bot = FakeBot(
        b"",
        error=ApiTelegramException(
            "getFile", 400, {"error_code": 400, "description": "Bad Request"}
        ),
    )
    kb = _make_kb(tmp_path)
    llm = ScriptedLLM([])
    conv = _make_conv(llm)
    replies: list[str] = []
    _handle_document(bot, kb, conv, _make_doc_message("notes.txt"), replies)
    assert replies == [_DOC_ERROR_REPLY]
    assert llm.chat_calls == []


def test_api_error_on_download_file_friendly_reply(tmp_path: Path) -> None:
    bot = FakeBot(
        b"",
        download_error=ApiTelegramException(
            "downloadFile", 400, {"error_code": 400, "description": "Bad Request"}
        ),
    )
    kb = _make_kb(tmp_path)
    llm = ScriptedLLM([])
    conv = _make_conv(llm)
    replies: list[str] = []
    _handle_document(bot, kb, conv, _make_doc_message("notes.txt"), replies)
    assert replies == [_DOC_ERROR_REPLY]
    assert llm.chat_calls == []


def test_ingest_embedder_failure_friendly_reply(tmp_path: Path) -> None:
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path, embedder=FakeEmbedder(error=EmbeddingError("down")))
    llm = ScriptedLLM([])
    conv = _make_conv(llm)
    replies: list[str] = []
    _handle_document(bot, kb, conv, _make_doc_message("notes.txt"), replies)
    assert replies == [_DOC_ERROR_REPLY]
    assert bot.get_file_calls == [_FILE_ID]
    assert llm.chat_calls == []


def test_unexpected_ingest_error_friendly_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected error inside the ingest boundary (SQLite/I/O/vector-index)
    must not escape into the polling loop: exactly one friendly error reply,
    no agent run."""
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    llm = ScriptedLLM([])
    conv = _make_conv(llm)
    replies: list[str] = []

    def locked(doc_id, chunks, meta=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kb.store, "replace_chunks", locked)
    _handle_document(bot, kb, conv, _make_doc_message("notes.txt"), replies)
    assert replies == [_DOC_ERROR_REPLY]
    assert bot.get_file_calls == [_FILE_ID]
    assert bot.download_calls == [_FILE_PATH]
    assert llm.chat_calls == []


def test_missing_file_path_friendly_error_no_download(tmp_path: Path) -> None:
    """get_file returning a File without a file_path → one friendly error,
    download never attempted (defensive narrowing is covered)."""
    bot = FakeBot(_DOC_BODY.encode(), file_path=None)
    kb = _make_kb(tmp_path)
    llm = ScriptedLLM([])
    conv = _make_conv(llm)
    replies: list[str] = []
    _handle_document(bot, kb, conv, _make_doc_message("notes.txt"), replies)
    assert replies == [_DOC_ERROR_REPLY]
    assert bot.get_file_calls == [_FILE_ID]
    assert bot.download_calls == []
    assert llm.chat_calls == []


def test_caption_non_llm_error_still_gets_friendly_reply(tmp_path: Path) -> None:
    """A non-LLMError escaping conv.handle (agent/registry bug) after a
    successful ingest must not escape into the polling loop: exactly one
    friendly caption reply, after the confirmation."""
    conv = _make_conv(ScriptedLLM([]))  # exhausted script → IndexError
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    replies: list[str] = []
    message = _make_doc_message("notes.txt", caption="Summarize this")
    _handle_document(bot, kb, conv, message, replies)
    assert len(replies) == 2
    assert replies[0].startswith(_CONFIRMATION_PREFIX)
    assert replies[1] == _LLM_ERROR_REPLY


def test_document_wins_over_text(tmp_path: Path) -> None:
    """A message with BOTH document and text takes the document path —
    the plain text is never sent to the LLM; the agent gets the upload
    notice instead."""
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    llm = FakeLLM(reply="text answer")
    conv = _make_conv(llm)
    handler = build_document_handler(bot, kb, conv)
    replies: list[str] = []
    message = _make_doc_message("notes.txt")
    message.text = "hello as plain text"
    handle_message(message, conv, _capture(replies), None, handler)
    assert bot.get_file_calls == [_FILE_ID]
    assert bot.download_calls == [_FILE_PATH]
    assert len(replies) == 2
    assert replies[0].startswith(_CONFIRMATION_PREFIX)
    assert replies[1] == "text answer"
    assert len(llm.chat_calls) == 1
    messages, _tools, _system = llm.chat_calls[0]
    assert "hello as plain text" not in messages[-1].content
    assert "notes.txt" in messages[-1].content
    assert llm.calls == []


def test_document_upload_propagates_context_to_next_message(tmp_path: Path) -> None:
    """Core regression: a no-caption upload enters conversation context —
    a follow-up message's LLM history contains the upload turn."""
    llm = ScriptedLLM(
        [ChatResponse(content="ack"), ChatResponse(content="it has needles")]
    )
    conv = _make_conv(llm)
    bot = FakeBot(_DOC_BODY.encode())
    kb = _make_kb(tmp_path)
    replies: list[str] = []
    _handle_document(bot, kb, conv, _make_doc_message("notes.txt"), replies)
    assert len(replies) == 2
    assert replies[0].startswith(_CONFIRMATION_PREFIX)
    assert replies[1] == "ack"
    follow_up = conv.handle(42, "what was in the document?")
    assert follow_up == "it has needles"
    history, _tools, _system = llm.chat_calls[1]
    assert any(m.role == "user" and "notes.txt" in m.content for m in history)


# --- Composition roots ---------------------------------------------------------------


def test_create_bot_kb_none_builds_bot(tmp_path: Path) -> None:
    conv = _make_conv(ScriptedLLM([]))
    created = create_bot(conv, kb=None)
    assert created is not None


def test_create_bot_with_kb_wires_document_handler(tmp_path: Path, monkeypatch) -> None:
    kb = _make_kb(tmp_path)
    conv = _make_conv(ScriptedLLM([]))
    seen: dict[str, object] = {}

    def fake_builder(b, stack, manager):
        seen["bot"] = b
        seen["kb"] = stack
        seen["conv"] = manager
        return lambda message, reply_to: None

    monkeypatch.setattr(bot_module, "build_document_handler", fake_builder)
    created = create_bot(conv, kb=kb)
    assert created is not None
    assert seen["bot"] is created
    assert seen["kb"] is kb
    assert seen["conv"] is conv


def test_create_bot_without_kb_skips_document_handler(monkeypatch) -> None:
    def unexpected_builder(*args: object) -> None:
        raise AssertionError("build_document_handler must not run when kb is None")

    monkeypatch.setattr(bot_module, "build_document_handler", unexpected_builder)
    created = create_bot(_make_conv(ScriptedLLM([])), kb=None)
    assert created is not None


def test_create_bot_registers_document_content_type(tmp_path: Path) -> None:
    """Regression: the catch-all handler must accept document messages, not
    just text — TeleBot defaults content_types to ['text'] when omitted,
    which silently drops document uploads before handle_message runs."""
    kb = _make_kb(tmp_path)
    conv = _make_conv(ScriptedLLM([]))
    tb = create_bot(conv, kb=kb)
    assert tb.message_handlers, "no message handlers registered"
    content_types = tb.message_handlers[0]["filters"]["content_types"]
    assert "document" in content_types
    assert "text" in content_types

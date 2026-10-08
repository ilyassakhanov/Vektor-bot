"""Telegram bot with long-polling.

Run with: python bot.py
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import telebot
from telebot.apihelper import ApiTelegramException

import config
import documents
import logging_config
import metrics
from agent.agent import Agent
from agent.conversation import ConversationManager
from documents import DocumentError
from llm import LLM, LLMError, OllamaLLM
from llm.instrumented import InstrumentedLLM
from retrieval.config import RetrievalConfig
from retrieval.embeddings import OllamaEmbedder
from retrieval.expansion import QueryExpander
from retrieval.hybrid import FtsSearch, HybridRetriever, VectorSearch
from retrieval.principal import reset_user, set_user
from retrieval.rerank import Reranker
from retrieval.store import META_EMBED_MODEL, ChunkStore, KBModelError
from skills.loader import SkillLoader
from tools.base import ToolError
from tools.exec import ExecTool
from tools.instrumented_registry import InstrumentedToolRegistry
from tools.kb import (
    KbIngestTool,
    KbSearchTool,
    KbStack,
    StoreFtsAdapter,
    StoreVecAdapter,
)
from tools.mcp import McpClient, McpStdioClient, McpTool, build_server_env
from tools.registry import ToolRegistry

# Load secrets from .env into the environment (real env vars take precedence).
config.load_env()
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

log = logging.getLogger("vektor.bot")

_PROJECT_ROOT = Path(__file__).resolve().parent
_DEFAULT_MAX_ITERATIONS = 8
_DEFAULT_METRICS_PORT = 9100
_DEFAULT_MCP_COMMAND: list[str] = [
    sys.executable,
    str(_PROJECT_ROOT / "mcp_servers" / "cve_server.py"),
]
_DOCUMENT_ERROR_REPLY = "Sorry, I couldn't process that document."
_LLM_ERROR_REPLY = "Sorry, I couldn't generate a response."
_DOCUMENT_FALLBACK_NAME = "document"
_AUTH_DENIED_REPLY = "Sorry, you are not allowed to use this bot."
_DOCUMENT_RECEIVED_REPLY = "📄 Document received"
_EXTRACTING_REPLY = "⏳ Extracting text…"
_EMBEDDING_REPLY = "⏳ Generating embeddings…"
_INGESTED_OK_PREFIX = "✅ "
_DOCUMENT_READY_REPLY = "✅ Document ready. Now you can ask questions."
_KB_DISABLED_REPLY = "The knowledge base is not enabled."
_DOCUMENTS_HEADER = "📚 Your documents:"
_DOCUMENTS_EMPTY_REPLY = (
    "📚 Your library is empty — upload a document (.txt, .md, .pdf, .docx)"
    " and it becomes searchable."
)
_DELETE_USAGE_REPLY = "Usage: /delete <filename>"


def build_llm() -> LLM:
    """Composition root — pick the LLM provider from configuration."""
    return InstrumentedLLM(
        OllamaLLM(
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            model=os.environ.get("OLLAMA_MODEL", "llama3.2"),
        )
    )


def build_kb_stack(cfg: RetrievalConfig | None = None) -> KbStack | None:
    """Compose the knowledge-base stack; None when KB_ENABLED=0.

    Wires store + embedder + FTS + expansion/rerank LLMs + retriever; the
    embedding model is validated on every start (conflict → KBModelError).
    """
    cfg = cfg or RetrievalConfig.from_env()
    if not cfg.kb_enabled:
        log.info("knowledge base disabled (KB_ENABLED=0)")
        return None
    cfg.kb_db_path.parent.mkdir(parents=True, exist_ok=True)
    store = ChunkStore(cfg.kb_db_path)
    stored_model = store.get_meta(META_EMBED_MODEL)
    if stored_model is None:
        store.set_meta(META_EMBED_MODEL, cfg.ollama_embed_model)
    elif stored_model != cfg.ollama_embed_model:
        store.close()
        raise KBModelError(
            f"Knowledge base at {cfg.kb_db_path} was built with embedding"
            f" model {stored_model!r}, but OLLAMA_EMBED_MODEL is now"
            f" {cfg.ollama_embed_model!r}. Restore the previous model or"
            " delete the database file to rebuild the knowledge base."
        )
    embedder = OllamaEmbedder(
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        model=cfg.ollama_embed_model,
    )
    fts: FtsSearch | None = None
    if store.fts_available and cfg.kb_fts_enabled:
        fts = StoreFtsAdapter(store)
    elif cfg.kb_fts_enabled:
        log.warning("KB_FTS_ENABLED=1 but FTS5 is unavailable; running vector-only")
    vector: VectorSearch = StoreVecAdapter(store)
    expander: QueryExpander | None = None
    if cfg.kb_expansion_enabled:
        expander = QueryExpander(
            OllamaLLM(
                base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
                model=cfg.ollama_expansion_model,
                timeout=cfg.kb_expansion_timeout,
                temperature=cfg.kb_expansion_temperature,
            )
        )
    reranker: Reranker | None = None
    if cfg.kb_rerank_enabled:
        reranker = Reranker(
            OllamaLLM(
                base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
                model=cfg.ollama_expansion_model,
                timeout=cfg.kb_rerank_timeout,
            )
        )
    retriever = HybridRetriever(
        embedder=embedder,
        vector=vector,
        fts=fts,
        expander=expander,
        reranker=reranker,
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
        expander=expander,
        reranker=reranker,
    )


def build_retriever(cfg: RetrievalConfig | None = None) -> HybridRetriever | None:
    """Build only the HybridRetriever; None when the knowledge base is off.

    Convenience wrapper over :func:`build_kb_stack`; the stack's resources
    are not closed automatically — prefer ``build_kb_stack`` + ``close()``.
    """
    stack = build_kb_stack(cfg)
    return None if stack is None else stack.retriever


def build_tool_registry(
    mcp_client: McpClient | None = None,
    kb: KbStack | None = None,
    auto_build_kb: bool = True,
) -> ToolRegistry:
    """Build the tool registry with all available tools.

    ``kb=None`` + ``auto_build_kb`` auto-builds the stack (failure → kb-less
    registry; KBModelError propagates; False = degraded, never retried).
    """
    reg = InstrumentedToolRegistry()
    timeout = float(os.environ.get("EXEC_TIMEOUT", "30"))
    reg.register(ExecTool(timeout=timeout))
    if mcp_client is not None:
        for spec in mcp_client.specs():
            reg.register(McpTool(client=mcp_client, spec=spec))
    if kb is None and auto_build_kb:
        try:
            kb = build_kb_stack()
        except KBModelError:
            raise
        except Exception:
            log.warning(
                "knowledge base unavailable; running without kb tools",
                exc_info=True,
            )
            kb = None
    if kb is not None:
        reg.register(
            KbIngestTool(
                store=kb.store,
                embedder=kb.embedder,
                chunk_size=kb.cfg.kb_chunk_size,
                chunk_overlap=kb.cfg.kb_chunk_overlap,
            )
        )
        reg.register(KbSearchTool(retriever=kb.retriever))
    return reg


def build_agent(
    llm: LLM,
    tools: ToolRegistry,
    max_iterations: int | None = None,
) -> Agent:
    """Build the agent with skills loaded from the skills/ directory."""
    skills_dir = _PROJECT_ROOT / "skills"
    loader = SkillLoader(skills_dir)
    system_prompt = loader.system_prompt()
    if max_iterations is None:
        max_iterations = int(
            os.environ.get("AGENT_MAX_ITERATIONS", str(_DEFAULT_MAX_ITERATIONS))
        )
    return Agent(
        llm=llm,
        tools=tools,
        system_prompt=system_prompt,
        max_iterations=max_iterations,
    )


def build_conversation_manager(
    llm: LLM,
    tools: ToolRegistry | None = None,
    max_iterations: int | None = None,
) -> ConversationManager:
    """Build a ConversationManager wired to an Agent."""
    if tools is None:
        tools = build_tool_registry(None)
    agent = build_agent(llm, tools, max_iterations=max_iterations)
    return ConversationManager(agent)


def load_allowed_usernames() -> frozenset[str]:
    """Parse ``ALLOWED_USERNAMES`` (comma-separated Telegram tags, e.g. ``@user``)."""
    raw = os.environ.get("ALLOWED_USERNAMES", "").strip()
    if not raw:
        return frozenset()
    return frozenset(
        part.strip().lstrip("@").lower() for part in raw.split(",") if part.strip()
    )


def _metrics_port_from_env() -> int:
    """Read ``METRICS_PORT`` — falls back to the default on invalid values."""
    raw = os.environ.get("METRICS_PORT")
    if raw is None:
        return _DEFAULT_METRICS_PORT
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "Invalid METRICS_PORT %r, using default %d", raw, _DEFAULT_METRICS_PORT
        )
        return _DEFAULT_METRICS_PORT


def _mcp_command_from_env() -> list[str]:
    """Read ``MCP_CVE_SERVER_CMD`` — falls back to the default when unset/empty."""
    raw = os.environ.get("MCP_CVE_SERVER_CMD")
    if not raw or not raw.strip():
        if raw is not None:
            log.warning(
                "Empty MCP_CVE_SERVER_CMD, using default %s",
                _DEFAULT_MCP_COMMAND,
            )
        return list(_DEFAULT_MCP_COMMAND)
    return raw.split()


def _is_authorized(
    message: telebot.types.Message,
    allowed_usernames: set[str] | frozenset[str] | None,
) -> bool:
    """Auth gate — True when the sender may use the bot (None = auth off)."""
    if allowed_usernames is None:
        return True
    user = getattr(message, "from_user", None)
    username: str | None = getattr(user, "username", None) if user is not None else None
    if not username:
        return False
    return username.lower() in allowed_usernames


def _principal_id(message: telebot.types.Message) -> str:
    """KB owner id — the sender's numeric Telegram id (``"0"`` when absent)."""
    user = getattr(message, "from_user", None)
    return str(user.id) if user is not None else "0"


def _format_date(created_at: str) -> str:
    """Render an ISO-8601 timestamp as ``YYYY-MM-DD`` (raw string fallback)."""
    try:
        return datetime.fromisoformat(created_at).strftime("%Y-%m-%d")
    except ValueError:
        return created_at


def handle_documents_command(
    message: telebot.types.Message,
    reply_to: Callable[..., Any],
    allowed_usernames: set[str] | frozenset[str] | None,
    store: ChunkStore | None,
) -> None:
    """``/documents`` — numbered list of the caller's documents.

    Auth gate runs FIRST; without a store (KB disabled) a friendly
    not-enabled reply is sent instead of touching storage.
    """
    if not _is_authorized(message, allowed_usernames):
        log.warning("unauthorized user=%s denied /documents", _principal_id(message))
        reply_to(message, _AUTH_DENIED_REPLY)
        return
    if store is None:
        reply_to(message, _KB_DISABLED_REPLY)
        return
    rows = store.list_documents(_principal_id(message))
    if not rows:
        reply_to(message, _DOCUMENTS_EMPTY_REPLY)
        return
    lines = [_DOCUMENTS_HEADER]
    for position, row in enumerate(rows, start=1):
        lines.append(
            f"{position}. {row.filename} — {_format_date(row.created_at)},"
            f" {row.chunk_count} chunks"
        )
    reply_to(message, "\n".join(lines))


def handle_delete_command(
    message: telebot.types.Message,
    reply_to: Callable[..., Any],
    allowed_usernames: set[str] | frozenset[str] | None,
    store: ChunkStore | None,
) -> None:
    """``/delete <filename>`` — remove the caller's document (auth gate first).

    Missing filename gets a usage hint; ``delete_document`` returning False
    gets a not-found reply — never a distinction an attacker could probe.
    """
    if not _is_authorized(message, allowed_usernames):
        log.warning("unauthorized user=%s denied /delete", _principal_id(message))
        reply_to(message, _AUTH_DENIED_REPLY)
        return
    if store is None:
        reply_to(message, _KB_DISABLED_REPLY)
        return
    parts = (getattr(message, "text", None) or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        reply_to(message, _DELETE_USAGE_REPLY)
        return
    filename = parts[1].strip()
    if store.delete_document(_principal_id(message), filename):
        reply_to(message, f'🗑 Deleted "{filename}" — chunks and embeddings are gone.')
    else:
        reply_to(message, f'❌ No document named "{filename}" in your library.')


def build_document_handler(
    bot: telebot.TeleBot,
    kb: KbStack,
    conv: ConversationManager,
) -> Callable[..., None]:
    """Build the document-message handler: staged progress → ingest → agent.

    Downloads/extracts/ingests BEFORE any agent run via ``ingest_document``
    (pages only for PDFs); failures end with exactly one friendly reply.
    """

    def handle_document(
        message: telebot.types.Message,
        reply_to: Callable[..., None],
    ) -> None:
        sender = getattr(message, "from_user", None)
        principal_token = set_user(str(sender.id) if sender is not None else "0")
        try:
            doc = message.document
            if doc is None:
                log.warning("document message without a document payload")
                reply_to(message, _DOCUMENT_ERROR_REPLY)
                return
            file_id = doc.file_id
            file_name = doc.file_name or _DOCUMENT_FALLBACK_NAME
            reply_to(message, _DOCUMENT_RECEIVED_REPLY)
            reply_to(message, _EXTRACTING_REPLY)
            try:
                file_info = bot.get_file(file_id)
                file_path = file_info.file_path
                if not file_path:
                    raise DocumentError("Telegram returned no file path")
                content = bot.download_file(file_path)
                file_type = Path(file_name).suffix.lstrip(".").lower()
                pages = documents.extract_pages(content, file_name)
                text = "\n".join(page_text for _, page_text in pages if page_text)
                reply_to(message, _EMBEDDING_REPLY)
                ingest = KbIngestTool(
                    store=kb.store,
                    embedder=kb.embedder,
                    chunk_size=kb.cfg.kb_chunk_size,
                    chunk_overlap=kb.cfg.kb_chunk_overlap,
                )
                ingest_result = ingest.ingest_document(
                    text=text,
                    title=file_name,
                    filename=file_name,
                    file_type=file_type,
                    pages=pages if file_type == "pdf" else None,
                )
            except (ApiTelegramException, DocumentError, ToolError) as exc:
                log.warning(
                    "document ingest failed for %r: %s", file_name, type(exc).__name__
                )
                reply_to(message, _DOCUMENT_ERROR_REPLY)
                return
            except Exception:
                log.warning(
                    "document ingest failed for %r: unexpected error",
                    file_name,
                    exc_info=True,
                )
                reply_to(message, _DOCUMENT_ERROR_REPLY)
                return
            log.info(
                "document %r ingested: %s chunks",
                file_name,
                _chunk_count(ingest_result),
            )
            reply_to(message, f"{_INGESTED_OK_PREFIX}{ingest_result}")
            reply_to(message, _DOCUMENT_READY_REPLY)
            notice = (
                f'[document uploaded: "{file_name}", '
                f"{_chunk_count(ingest_result)} chunks ingested; "
                "content is now searchable via kb_search]"
            )
            caption = (getattr(message, "caption", None) or "").strip()
            prompt = f"{notice}\n\n{caption}" if caption else notice
            try:
                response = conv.handle(message.chat.id, prompt)
            except LLMError as exc:
                log.warning("LLM error on document upload: %s", exc)
                reply_to(message, _LLM_ERROR_REPLY)
                return
            except Exception:
                log.warning("agent error on document upload", exc_info=True)
                reply_to(message, _LLM_ERROR_REPLY)
                return
            reply_to(message, response)
        finally:
            reset_user(principal_token)

    return handle_document


def _chunk_count(ingest_result: str) -> str:
    """Extract the chunk count from a ``KbIngestTool`` result string."""
    match = re.search(r"Ingested (\d+) chunks", ingest_result)
    return match.group(1) if match else "unknown"


def handle_message(
    message: telebot.types.Message,
    conv: ConversationManager,
    reply_to,
    allowed_usernames: set[str] | frozenset[str] | None = None,
    document_handler: Callable[..., None] | None = None,
) -> None:
    """Process a single message: route through the Agent and reply via ``reply_to``.

    Auth gate FIRST; the KB principal is set from ``from_user.id`` after it and
    reset in ``finally``; documents go to ``document_handler`` (None = disabled).
    """
    user = message.from_user
    log.info(
        "message id=%s chat=%s user=%s%s",
        message.message_id,
        message.chat.id,
        user.id if user else "?",
        f" @{user.username}" if user and user.username else "",
    )
    if not _is_authorized(message, allowed_usernames):
        log.warning("unauthorized user=%s denied", user.id if user else "?")
        reply_to(message, _AUTH_DENIED_REPLY)
        return
    principal_token = set_user(str(user.id) if user is not None else "0")
    try:
        doc = getattr(message, "document", None)
        if doc is not None:
            if document_handler is None:
                reply_to(message, "Document uploads are not enabled.")
                return
            document_handler(message, reply_to)
            return
        try:
            response = conv.handle(message.chat.id, message.text or "")
        except LLMError as exc:
            log.warning("LLM error: %s", exc)
            reply_to(message, _LLM_ERROR_REPLY)
            return
        reply_to(message, response)
    finally:
        reset_user(principal_token)


def create_bot(
    conv: ConversationManager,
    allowed_usernames: frozenset[str] | None = None,
    kb: KbStack | None = None,
) -> telebot.TeleBot:
    """Wire up a TeleBot with the injected ConversationManager.

    Command handlers (``/documents``, ``/delete``) register BEFORE the catch-all
    handler (TeleBot tests in registration order); with ``kb``, uploads enabled.
    """
    bot = telebot.TeleBot(BOT_TOKEN)
    store = kb.store if kb is not None else None
    document_handler = build_document_handler(bot, kb, conv) if kb is not None else None

    @bot.message_handler(commands=["documents"])
    def on_documents(message: telebot.types.Message) -> None:
        handle_documents_command(message, bot.reply_to, allowed_usernames, store)

    @bot.message_handler(commands=["delete"])
    def on_delete(message: telebot.types.Message) -> None:
        handle_delete_command(message, bot.reply_to, allowed_usernames, store)

    @bot.message_handler(func=lambda m: True, content_types=["text", "document"])
    def on_message(message: telebot.types.Message) -> None:
        handle_message(message, conv, bot.reply_to, allowed_usernames, document_handler)

    return bot


def main() -> None:
    logging_config.configure_logging(
        loki_url=os.environ.get("LOKI_PUSH_URL") or None,
        level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    metrics_port = _metrics_port_from_env()
    metrics.start_metrics_server(metrics_port)
    llm = build_llm()
    mcp_client = McpStdioClient(
        command=_mcp_command_from_env(),
        env=build_server_env(),
        startup_timeout=float(os.environ.get("MCP_STARTUP_TIMEOUT", "10")),
        cwd=str(_PROJECT_ROOT),
        pythonpath=str(_PROJECT_ROOT),
    )
    kb: KbStack | None = None
    try:
        try:
            kb = build_kb_stack()
        except KBModelError as exc:
            log.error("%s", exc)
            raise SystemExit(1) from exc
        except Exception:
            log.warning(
                "knowledge base unavailable; running without kb tools",
                exc_info=True,
            )
        try:
            mcp_client.start()
            reg = build_tool_registry(mcp_client, kb=kb, auto_build_kb=False)
        except Exception:
            log.warning(
                "MCP CVE server failed to start; running exec-only", exc_info=True
            )
            metrics.mcp_server_up.set(0)
            reg = build_tool_registry(None, kb=kb, auto_build_kb=False)
        conv = build_conversation_manager(llm, tools=reg)
        allowed = load_allowed_usernames()
        if allowed:
            log.info("Allowed users: %d", len(allowed))
        else:
            log.warning("No ALLOWED_USERNAMES set — all users denied.")
        bot = create_bot(conv, allowed, kb=kb)
        log.info("Starting bot (polling)...")
        try:
            bot.infinity_polling()
        except KeyboardInterrupt:
            log.info("Stopped by user.")
        finally:
            bot.stop_polling()
            log.info("Bot shut down.")
    finally:
        mcp_client.stop()
        if kb is not None:
            kb.close()


if __name__ == "__main__":
    main()

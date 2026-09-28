"""Telegram bot with long-polling.

Run with: python bot.py
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

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
from retrieval.rrf import ChunkHit
from retrieval.store import ChunkStore
from retrieval.vector_index import VectorIndex
from skills.loader import SkillLoader
from tools.base import ToolError
from tools.exec import ExecTool
from tools.instrumented_registry import InstrumentedToolRegistry
from tools.kb import (
    KbIngestTool,
    KbSearchTool,
    KbStack,
    StoreFtsAdapter,
    VectorIndexAdapter,
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

    Ensures the DB parent directory exists (the store does not), builds the
    ChunkStore, OllamaEmbedder (model from ``OLLAMA_EMBED_MODEL``), a
    VectorIndex seeded from the store's vectors, the FTS adapter (only when
    FTS5 is available AND ``KB_FTS_ENABLED`` — unavailable-but-enabled logs
    a warning and degrades to vector-only), the expansion LLM (only when
    ``KB_EXPANSION_ENABLED`` — a second OllamaLLM with
    ``OLLAMA_EXPANSION_MODEL``, temperature, and short timeout), and the
    HybridRetriever wired with every config limit.
    """
    cfg = cfg or RetrievalConfig.from_env()
    if not cfg.kb_enabled:
        log.info("knowledge base disabled (KB_ENABLED=0)")
        return None
    cfg.kb_db_path.parent.mkdir(parents=True, exist_ok=True)
    store = ChunkStore(cfg.kb_db_path)
    embedder = OllamaEmbedder(
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        model=cfg.ollama_embed_model,
    )
    vector_index = VectorIndex(dict(store.all_vectors()))
    metadata: dict[str, ChunkHit] = {}
    fts: FtsSearch | None = None
    if store.fts_available and cfg.kb_fts_enabled:
        fts = StoreFtsAdapter(store)
    elif cfg.kb_fts_enabled:
        log.warning("KB_FTS_ENABLED=1 but FTS5 is unavailable; running vector-only")
    vector: VectorSearch = VectorIndexAdapter(vector_index, metadata)
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
    retriever = HybridRetriever(
        embedder=embedder,
        vector=vector,
        fts=fts,
        expander=expander,
        vector_limit=cfg.kb_vector_limit,
        fts_limit=cfg.kb_fts_limit,
        top_k=cfg.kb_top_k,
        rrf_k=cfg.kb_rrf_k,
    )
    return KbStack(
        cfg=cfg,
        store=store,
        embedder=embedder,
        vector_index=vector_index,
        vector=vector,
        retriever=retriever,
        metadata=metadata,
        fts=fts,
        expander=expander,
    )


def build_retriever(cfg: RetrievalConfig | None = None) -> HybridRetriever | None:
    """Build only the HybridRetriever; None when the knowledge base is off.

    Convenience wrapper over :func:`build_kb_stack` for callers that need
    just the retriever. The stack's resources (store, embedder) are not
    closed automatically — long-lived processes should use
    ``build_kb_stack`` + ``KbStack.close()`` instead.
    """
    stack = build_kb_stack(cfg)
    return None if stack is None else stack.retriever


def build_tool_registry(
    mcp_client: McpClient | None = None,
    kb: KbStack | None = None,
) -> ToolRegistry:
    """Build the tool registry with all available tools.

    When ``kb`` is None the knowledge-base stack is auto-built from the
    environment (``KB_ENABLED``, default on), so ``python bot.py`` works
    unchanged; an auto-build failure degrades to a kb-less registry with a
    warning, mirroring the MCP fallback. Pass an explicit stack to inject
    test doubles. ``KB_ENABLED=0`` yields the exact pre-kb tool set.
    """
    reg = InstrumentedToolRegistry()
    timeout = float(os.environ.get("EXEC_TIMEOUT", "30"))
    reg.register(ExecTool(timeout=timeout))
    if mcp_client is not None:
        for spec in mcp_client.specs():
            reg.register(McpTool(client=mcp_client, spec=spec))
    if kb is None:
        try:
            kb = build_kb_stack()
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
                vector_index=kb.vector_index,
                metadata=kb.metadata,
                chunk_size=kb.cfg.kb_chunk_size,
                chunk_overlap=kb.cfg.kb_chunk_overlap,
            )
        )
        reg.register(KbSearchTool(retriever=kb.retriever, store=kb.store))
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


def build_document_handler(
    bot: telebot.TeleBot,
    kb: KbStack,
    conv: ConversationManager,
) -> Callable[..., None]:
    """Build the document-message handler: download → extract → ingest → reply.

    The document is ingested via a fresh :class:`KbIngestTool` (same wiring
    as ``build_tool_registry``) BEFORE any agent run — extraction output
    must never cross the LLM tool boundary as an argument. If the document
    carries a non-empty caption, the caption is routed through ``conv`` and
    the agent's response is sent as a second reply, so the agent can
    immediately ``kb_search`` the ingested content. Download, extraction,
    and ingest failures each produce exactly one user-friendly reply and
    are never re-raised into the polling loop. Logs carry file name and
    outcome only — never file content or captions.
    """

    def handle_document(
        message: telebot.types.Message,
        reply_to: Callable[..., None],
    ) -> None:
        doc = message.document
        if doc is None:
            log.warning("document message without a document payload")
            reply_to(message, _DOCUMENT_ERROR_REPLY)
            return
        file_id = doc.file_id
        file_name = doc.file_name or _DOCUMENT_FALLBACK_NAME
        try:
            file_info = bot.get_file(file_id)
            file_path = file_info.file_path
            if not file_path:
                raise DocumentError("Telegram returned no file path")
            content = bot.download_file(file_path)
            text = documents.extract_text(content, file_name)
            ingest = KbIngestTool(
                store=kb.store,
                embedder=kb.embedder,
                vector_index=kb.vector_index,
                metadata=kb.metadata,
                chunk_size=kb.cfg.kb_chunk_size,
                chunk_overlap=kb.cfg.kb_chunk_overlap,
            )
            ingest_result = ingest.execute(text=text, title=file_name)
        except (ApiTelegramException, DocumentError, ToolError) as exc:
            log.warning(
                "document ingest failed for %r: %s", file_name, type(exc).__name__
            )
            reply_to(message, _DOCUMENT_ERROR_REPLY)
            return
        log.info(
            "document %r ingested: %s chunks",
            file_name,
            _chunk_count(ingest_result),
        )
        reply_to(message, ingest_result)
        caption = (getattr(message, "caption", None) or "").strip()
        if not caption:
            return
        try:
            response = conv.handle(message.chat.id, caption)
        except LLMError as exc:
            log.warning("LLM error on document caption: %s", exc)
            reply_to(message, _LLM_ERROR_REPLY)
            return
        except Exception:
            log.warning("agent error on document caption", exc_info=True)
            reply_to(message, _LLM_ERROR_REPLY)
            return
        reply_to(message, response)

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

    ``reply_to`` is the callable used to send a reply (typically
    ``bot.reply_to``); tests inject a fake.

    When ``allowed_usernames`` is provided, only users whose Telegram username
    is in that set may use the bot; others get a denial reply.

    Document messages are delegated to ``document_handler`` (before the text
    path) — a document never reaches the agent as message text. When
    ``document_handler`` is None (knowledge base disabled), documents get a
    "not enabled" reply. The auth check runs FIRST, so unauthorized users'
    documents are never downloaded.
    """
    user = message.from_user
    log.info(
        "message id=%s chat=%s user=%s%s",
        message.message_id,
        message.chat.id,
        user.id if user else "?",
        f" @{user.username}" if user and user.username else "",
    )
    if allowed_usernames is not None and (
        user is None
        or not user.username
        or user.username.lower() not in allowed_usernames
    ):
        log.warning("unauthorized user=%s denied", user.id if user else "?")
        reply_to(message, "Sorry, you are not allowed to use this bot.")
        return
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


def create_bot(
    conv: ConversationManager,
    allowed_usernames: frozenset[str] | None = None,
    kb: KbStack | None = None,
) -> telebot.TeleBot:
    """Wire up a TeleBot with the injected ConversationManager.

    When ``kb`` is provided, document uploads are enabled: the wired
    handler downloads → extracts → ingests documents into the knowledge
    base before the agent runs (see :func:`build_document_handler`).
    """
    bot = telebot.TeleBot(BOT_TOKEN)
    document_handler = build_document_handler(bot, kb, conv) if kb is not None else None

    @bot.message_handler(func=lambda m: True)
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
        except Exception:
            log.warning(
                "knowledge base unavailable; running without kb tools",
                exc_info=True,
            )
        try:
            mcp_client.start()
            reg = build_tool_registry(mcp_client, kb=kb)
        except Exception:
            log.warning(
                "MCP CVE server failed to start; running exec-only", exc_info=True
            )
            metrics.mcp_server_up.set(0)
            reg = build_tool_registry(None, kb=kb)
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

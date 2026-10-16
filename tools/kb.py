"""Knowledge-base agent tools — kb_ingest and kb_search.

``kb_ingest`` chunks + embeds + persists text (re-ingest = upsert); ``kb_search``
renders a compact fact sheet; the owner is the principal — never the LLM.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from retrieval.chunking import PageChunk, chunk_pages, chunk_text
from retrieval.config import RetrievalConfig
from retrieval.embeddings import Embedder, EmbeddingError
from retrieval.expansion import QueryExpander
from retrieval.hybrid import FtsSearch, HybridRetriever, VectorSearch
from retrieval.principal import get_user
from retrieval.rerank import Reranker
from retrieval.rrf import ChunkHit
from retrieval.store import (
    META_EMBED_DIM,
    ChunkRecord,
    ChunkStore,
    DocumentRecord,
    chunk_id_for,
    to_blob,
)
from tools.base import Tool, ToolError
from tools.truncation import max_output_chars_from_env, truncate

log = logging.getLogger("vektor.tools.kb")

_DOC_ID_HEX_CHARS = 16
_DEFAULT_TITLE_CHARS = 40
_SAMPLE_CHUNK_IDS = 3
_MAX_HIT_CONTENT_CHARS = 600
_EMPTY_RESULTS_REPLY = "No matching knowledge found."


class StoreFtsAdapter(FtsSearch):
    """FtsSearch adapter — bridges ``ChunkStore.search_fts`` to ``search``."""

    def __init__(self, store: ChunkStore) -> None:
        self._store = store

    def search(self, terms: list[str], limit: int, user_id: str) -> list[ChunkHit]:
        return self._store.search_fts(user_id, terms, limit)


class StoreVecAdapter(VectorSearch):
    """VectorSearch adapter — bridges ``ChunkStore.search_vec`` to ``search``."""

    def __init__(self, store: ChunkStore) -> None:
        self._store = store

    def search(
        self, queries: list[list[float]], limit: int, user_id: str
    ) -> list[ChunkHit]:
        return self._store.search_vec(user_id, queries, limit)


class KbIngestTool(Tool):
    """``kb_ingest`` — store text into the local knowledge base.

    LLM-facing ``execute`` honors ONLY ``{text, title}`` (any other kwarg raises
    ToolError — metadata cannot be forged); the bot ingests via ingest_document.
    """

    def __init__(
        self,
        store: ChunkStore,
        embedder: Embedder,
        chunk_size: int = 800,
        chunk_overlap: int = 100,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    @property
    def name(self) -> str:
        return "kb_ingest"

    @property
    def description(self) -> str:
        return (
            "Store text into the local knowledge base for later retrieval. "
            "Use when the user asks to remember, save, or index information; "
            "retrieve it later with kb_search."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to store.",
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Optional short label for the document "
                        "(defaults to the beginning of the text)."
                    ),
                },
            },
            "required": ["text"],
        }

    def execute(self, **kwargs: Any) -> str:
        unknown = set(kwargs) - {"text", "title"}
        if unknown:
            raise ToolError(
                "Unsupported argument(s): "
                + ", ".join(sorted(unknown))
                + " — kb_ingest accepts only text and title."
            )
        text = kwargs.get("text")
        if text is None:
            raise ToolError("Missing required argument: text")
        return self.ingest_document(
            text=str(text),
            title=str(kwargs.get("title") or ""),
            filename="",
            file_type="",
        )

    def ingest_document(
        self,
        text: str,
        title: str,
        filename: str,
        file_type: str,
        pages: list[tuple[int, str]] | None = None,
    ) -> str:
        """Bot-level ingestion entry: chunk, embed and persist ``text``.

        Unlike ``execute``, takes ``filename``/``file_type`` metadata and
        per-page ``pages`` (page numbers land in ``chunks.page``); owner = principal.
        """
        doc_id = hashlib.sha256(text.encode()).hexdigest()[:_DOC_ID_HEX_CHARS]
        resolved_title = self._resolve_title(title, text)
        owner = get_user()
        if pages is None:
            page_chunks = [
                PageChunk(page=None, text=chunk)
                for chunk in chunk_text(text, self._chunk_size, self._chunk_overlap)
            ]
        else:
            page_chunks = chunk_pages(pages, self._chunk_size, self._chunk_overlap)
        if not page_chunks:
            return f"Ingested 0 chunks (doc {doc_id}, no content)."
        try:
            vectors = self._embedder.embed([chunk.text for chunk in page_chunks])
        except EmbeddingError as exc:
            raise ToolError(
                "Could not ingest: the embedding service is unavailable. "
                "Try again later."
            ) from exc
        dim = len(vectors[0])
        self._validate_dim(dim)
        records = [
            ChunkRecord(
                document_id=doc_id,
                chunk_index=idx,
                text=page_chunk.text,
                embedding=to_blob(vector),
                page=page_chunk.page,
            )
            for idx, (page_chunk, vector) in enumerate(zip(page_chunks, vectors))
        ]
        self._store.add_document(
            DocumentRecord(
                id=doc_id,
                user_id=owner,
                filename=filename or resolved_title,
                file_type=file_type,
                created_at=datetime.now(UTC).isoformat(),
                chunks=records,
            )
        )
        log.info(
            "kb_ingest: stored %d chunks (doc %s, user %s)",
            len(records),
            doc_id,
            owner,
        )
        sample = ", ".join(
            chunk_id_for(doc_id, idx)
            for idx in range(min(_SAMPLE_CHUNK_IDS, len(page_chunks)))
        )
        return (
            f"Ingested {len(page_chunks)} chunks (doc {doc_id}, "
            f"title '{resolved_title}'). chunk_ids: {sample}"
        )

    def _validate_dim(self, dim: int) -> None:
        """Refuse to mix vectors of a different dimension than what is stored."""
        stored_dim = self._store.get_meta(META_EMBED_DIM)
        if stored_dim is None or self._parse_dim(stored_dim) == dim:
            return
        raise ToolError(
            "Could not ingest: the knowledge base stores vectors with"
            f" embedding dimension {stored_dim}, but the embedding"
            f" service returned dimension {dim}. The embedding model"
            " changed — restore the previous OLLAMA_EMBED_MODEL or"
            " delete the knowledge base database to rebuild it."
        )

    @staticmethod
    def _parse_dim(raw: str) -> int:
        """Parse a persisted ``embed_dim`` meta value; -1 when corrupt."""
        try:
            return int(raw)
        except ValueError:
            return -1

    @staticmethod
    def _resolve_title(title: str, text: str) -> str:
        if title.strip():
            return title.strip()
        normalized = " ".join(text.split())
        return normalized[:_DEFAULT_TITLE_CHARS] or "untitled"


class KbSearchTool(Tool):
    """``kb_search`` — hybrid retrieval over the local knowledge base.

    Renders a compact fact sheet (source tag, title, chunk/page, capped content;
    raw scores never shown); capped at ``EXEC_MAX_OUTPUT_CHARS`` (default 4000).
    """

    def __init__(
        self,
        retriever: HybridRetriever,
        max_output_chars: int | None = None,
    ) -> None:
        self._retriever = retriever
        self._max_output_chars = max_output_chars_from_env(max_output_chars)

    @property
    def name(self) -> str:
        return "kb_search"

    @property
    def description(self) -> str:
        return (
            "Search the local knowledge base for previously stored "
            "information. Use when the user asks about facts, notes, or "
            "documents that were saved earlier with kb_ingest."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for.",
                },
            },
            "required": ["query"],
        }

    def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query")
        if query is None:
            raise ToolError("Missing required argument: query")
        query = str(query)
        if not query.strip():
            raise ToolError("query must be a non-empty string.")

        result = self._retriever.search(query, get_user())

        if not result.hits:
            sheet = _EMPTY_RESULTS_REPLY
            if result.note:
                sheet = f"{sheet}\nnote: {result.note}"
            return truncate(sheet, self._max_output_chars)

        lines: list[str] = []
        for position, hit in enumerate(result.hits, start=1):
            sources = "+".join(hit.sources) if hit.sources else "unknown"
            page_part = f", page {hit.page}" if hit.page is not None else ""
            lines.append(
                f"{position}. [{sources}] {hit.title or 'untitled'}"
                f" (chunk {hit.idx}{page_part})"
            )
            lines.append(f"   {self._cap_content(hit.content)}")
        if result.used_expansion:
            lines.append("note: search query was expanded with additional terms.")
        if result.note:
            lines.append(f"note: {result.note}")
        return truncate("\n".join(lines), self._max_output_chars)

    @staticmethod
    def _cap_content(content: str) -> str:
        content = content.strip()
        if not content:
            return "(content unavailable)"
        if len(content) <= _MAX_HIT_CONTENT_CHARS:
            return content
        return content[: _MAX_HIT_CONTENT_CHARS - 3] + "..."


@dataclass
class KbStack:
    """Every retrieval piece the kb tools orchestrate, wired and shared.

    ``bot.build_kb_stack`` composes it, ``build_tool_registry`` turns it into
    agent tools; ``close()`` releases store/embedder/LLMs best-effort.
    """

    cfg: RetrievalConfig
    store: ChunkStore
    embedder: Embedder
    vector: VectorSearch
    retriever: HybridRetriever
    fts: FtsSearch | None = None
    expander: QueryExpander | None = None
    reranker: Reranker | None = None

    def close(self) -> None:
        """Best-effort release of store, embedder, expander and reranker."""
        try:
            self.store.close()
        except Exception:
            log.warning("failed to close kb store", exc_info=True)
        close_embedder = getattr(self.embedder, "close", None)
        if callable(close_embedder):
            try:
                close_embedder()
            except Exception:
                log.warning("failed to close kb embedder", exc_info=True)
        if self.expander is not None:
            try:
                self.expander.close()
            except Exception:
                log.warning("failed to close kb expander", exc_info=True)
        if self.reranker is not None:
            try:
                self.reranker.close()
            except Exception:
                log.warning("failed to close kb reranker", exc_info=True)

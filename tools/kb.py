"""Knowledge-base agent tools — kb_ingest and kb_search.

Two :class:`~tools.base.Tool` implementations expose the retrieval
subsystem to the agent:

* :class:`KbIngestTool` (``kb_ingest``) — chunk text, embed all chunks in
  one batch, persist them via :class:`~retrieval.store.ChunkStore`, and
  refresh the :class:`~retrieval.vector_index.VectorIndex` so searches see
  the new content immediately. The document id is ``sha256(text)[:16]``,
  making re-ingest of the same text an upsert (stable chunk ids) instead
  of a duplicate.
* :class:`KbSearchTool` (``kb_search``) — run the full
  :class:`~retrieval.hybrid.HybridRetriever` pipeline (expansion → vector
  ‖ FTS → RRF fusion) and format the fused hits as a compact fact sheet
  (position, title, chunk index, source tag, capped content); raw ranking
  scores are never shown. The whole sheet is capped with the shared
  :func:`~tools.truncation.truncate` (``EXEC_MAX_OUTPUT_CHARS``).

The searchable backends are bridged by two adapters:
:class:`StoreFtsAdapter` renames ``ChunkStore.search_fts`` to the
:class:`~retrieval.hybrid.FtsSearch` interface, and
:class:`VectorIndexAdapter` wraps :class:`~retrieval.vector_index.VectorIndex`
(which already matches :class:`~retrieval.hybrid.VectorSearch` duck-typed,
but is not a subclass) while hydrating its metadata-less hits — VectorIndex
returns chunk_id + score only, so the adapter joins chunk metadata from the
shared ``metadata`` cache that :class:`KbIngestTool` populates on ingest
(a fast path).

Metadata hydration has a second, authoritative layer:
:class:`KbSearchTool` joins full metadata from the ChunkStore onto fused
hits whose content is empty. This covers restarts — a fresh process
reloads vectors from the persistent DB but starts with an empty in-process
cache — and the hybrid case where an unhydrated vector-source stub would
otherwise displace FTS's full metadata in the fused hit. Embedding BLOBs
are encoded with :func:`~retrieval.vector_index.to_blob`, the single
float32 serialization point.

:class:`KbStack` bundles every wired piece (store, embedder, index,
adapters, expander, retriever, config) so ``bot.build_kb_stack`` can hand
the whole composition to ``bot.build_tool_registry`` — injectable for
tests, one ``close()`` for shutdown.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field, replace
from typing import Any

from retrieval.chunking import chunk_text
from retrieval.config import RetrievalConfig
from retrieval.embeddings import Embedder, EmbeddingError
from retrieval.expansion import QueryExpander
from retrieval.hybrid import FtsSearch, HybridRetriever, VectorSearch
from retrieval.rrf import ChunkHit, FusedHit
from retrieval.store import (
    META_EMBED_DIM,
    ChunkRecord,
    ChunkStore,
    chunk_id_for,
)
from retrieval.vector_index import VectorIndex, to_blob
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

    def search(self, terms: list[str], limit: int) -> list[ChunkHit]:
        return self._store.search_fts(terms, limit)


class VectorIndexAdapter(VectorSearch):
    """VectorSearch adapter over VectorIndex with chunk-metadata hydration.

    VectorIndex hits carry only chunk_id and score (its docstring defers
    metadata joining to callers). The adapter joins the shared metadata
    cache populated by :class:`KbIngestTool`: a cached entry is a fully
    populated :class:`~retrieval.rrf.ChunkHit` whose ``score`` is a
    placeholder, replaced by the real VectorIndex score on hydration.
    Uncached chunk ids (ingested by a previous process) pass through
    unchanged.
    """

    def __init__(self, index: VectorIndex, metadata: dict[str, ChunkHit]) -> None:
        self._index = index
        self._metadata = metadata

    def search(self, queries: list[list[float]], limit: int) -> list[ChunkHit]:
        return [self._hydrate(hit) for hit in self._index.search(queries, limit)]

    def _hydrate(self, hit: ChunkHit) -> ChunkHit:
        cached = self._metadata.get(hit.chunk_id)
        if cached is None:
            return hit
        return replace(cached, score=hit.score)


class KbIngestTool(Tool):
    """``kb_ingest`` — store text into the local knowledge base.

    Chunks the text (configured size/overlap), embeds ALL chunks in one
    :meth:`~retrieval.embeddings.Embedder.embed` batch, then — under the
    shared ``ingest_lock`` — validates the vector dimension against the
    persisted ``embed_dim`` meta (committing it atomically with the chunks
    on first ingest), replaces the document's stored chunks via
    :meth:`~retrieval.store.ChunkStore.replace_chunks`, refreshes the
    :class:`~retrieval.vector_index.VectorIndex` from the store, and
    updates the shared metadata cache (dropping cache entries of removed
    chunks). The lock serializes the complete write/snapshot/publication
    sequence across every :class:`KbIngestTool` sharing it (the bot wires
    one per :class:`KbStack`): TeleBot handlers run concurrently, and
    interleaved ingests could otherwise publish an older vector-index
    generation over a newer one. The embed call stays outside the lock —
    it is the slow, network-bound step. The document id is
    ``sha256(text)[:16]`` hex — stable per document, so the same text
    never duplicates and re-ingest removes obsolete chunks (e.g. a tail
    left by a previous chunking configuration). The title defaults to the
    first ~40 characters of the (whitespace-normalized) text, or
    ``untitled`` when the text starts with non-word content that
    normalizes away.
    """

    def __init__(
        self,
        store: ChunkStore,
        embedder: Embedder,
        vector_index: VectorIndex,
        metadata: dict[str, ChunkHit],
        chunk_size: int = 800,
        chunk_overlap: int = 100,
        ingest_lock: threading.Lock | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._vector_index = vector_index
        self._metadata = metadata
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._ingest_lock = ingest_lock or threading.Lock()

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
        text = kwargs.get("text")
        if text is None:
            raise ToolError("Missing required argument: text")
        text = str(text)
        title = str(kwargs.get("title") or "")
        return self._ingest(text, title)

    def _ingest(self, text: str, title: str) -> str:
        doc_id = hashlib.sha256(text.encode()).hexdigest()[:_DOC_ID_HEX_CHARS]
        resolved_title = self._resolve_title(title, text)
        chunks = chunk_text(text, self._chunk_size, self._chunk_overlap)
        if not chunks:
            return f"Ingested 0 chunks (doc {doc_id}, no content)."
        try:
            vectors = self._embedder.embed(chunks)
        except EmbeddingError as exc:
            raise ToolError(
                "Could not ingest: the embedding service is unavailable. "
                "Try again later."
            ) from exc
        dim = len(vectors[0])
        records = [
            ChunkRecord(
                doc_id=doc_id,
                title=resolved_title,
                idx=idx,
                content=chunk,
                embedding=to_blob(vector),
            )
            for idx, (chunk, vector) in enumerate(zip(chunks, vectors))
        ]
        with self._ingest_lock:
            stored_dim = self._store.get_meta(META_EMBED_DIM)
            if stored_dim is not None and self._parse_dim(stored_dim) != dim:
                raise ToolError(
                    "Could not ingest: the knowledge base stores vectors with"
                    f" embedding dimension {stored_dim}, but the embedding"
                    f" service returned dimension {dim}. The embedding model"
                    " changed — restore the previous OLLAMA_EMBED_MODEL or"
                    " delete the knowledge base database to rebuild it."
                )
            self._store.replace_chunks(
                doc_id,
                records,
                meta={META_EMBED_DIM: str(dim)} if stored_dim is None else None,
            )
            self._prune_metadata_cache(doc_id, records)
            self._vector_index.replace_all(dict(self._store.all_vectors()))
            for record in records:
                chunk_id = chunk_id_for(record.doc_id, record.idx)
                self._metadata[chunk_id] = ChunkHit(
                    chunk_id=chunk_id,
                    doc_id=record.doc_id,
                    title=record.title,
                    idx=record.idx,
                    content=record.content,
                    score=0.0,
                )
        log.info("kb_ingest: stored %d chunks (doc %s)", len(records), doc_id)
        sample = ", ".join(
            chunk_id_for(doc_id, idx)
            for idx in range(min(_SAMPLE_CHUNK_IDS, len(chunks)))
        )
        return (
            f"Ingested {len(chunks)} chunks (doc {doc_id}, "
            f"title '{resolved_title}'). chunk_ids: {sample}"
        )

    @staticmethod
    def _parse_dim(raw: str) -> int:
        """Parse a persisted ``embed_dim`` meta value; -1 when corrupt."""
        try:
            return int(raw)
        except ValueError:
            return -1

    def _prune_metadata_cache(self, doc_id: str, records: list[ChunkRecord]) -> None:
        """Drop cached entries of this document's removed chunks.

        ``replace_chunks`` may have deleted tail chunks from a previous
        chunking configuration; their cache entries would otherwise linger
        as hydratable ghosts.
        """
        keep = {chunk_id_for(doc_id, record.idx) for record in records}
        stale = [
            chunk_id
            for chunk_id, hit in self._metadata.items()
            if hit.doc_id == doc_id and chunk_id not in keep
        ]
        for chunk_id in stale:
            del self._metadata[chunk_id]

    @staticmethod
    def _resolve_title(title: str, text: str) -> str:
        if title.strip():
            return title.strip()
        normalized = " ".join(text.split())
        return normalized[:_DEFAULT_TITLE_CHARS] or "untitled"


class KbSearchTool(Tool):
    """``kb_search`` — hybrid retrieval over the local knowledge base.

    Runs :meth:`~retrieval.hybrid.HybridRetriever.search` (which never
    raises), joins full chunk metadata from the ChunkStore onto fused hits
    whose content is empty (the authoritative hydration layer covering
    restarts and unhydrated vector-source stubs; the in-process ingest
    cache remains a fast path), and formats the fused hits as a compact
    fact sheet: one ``[sources] title (chunk N)`` header plus capped
    content per hit. Raw RRF/cosine/BM25 scores are never shown. Expansion
    and degradation are surfaced only when meaningful, as ``note:`` lines.
    An empty result is the string ``"No matching knowledge found."`` —
    never an error. The whole sheet is capped at ``max_output_chars`` (env
    ``EXEC_MAX_OUTPUT_CHARS``, default 4000) with head+tail truncation.
    """

    def __init__(
        self,
        retriever: HybridRetriever,
        store: ChunkStore | None = None,
        max_output_chars: int | None = None,
    ) -> None:
        self._retriever = retriever
        self._store = store
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

        result = self._retriever.search(query)
        if not result.hits:
            sheet = _EMPTY_RESULTS_REPLY
            if result.note:
                sheet = f"{sheet}\nnote: {result.note}"
            return truncate(sheet, self._max_output_chars)

        lines: list[str] = []
        for position, hit in enumerate(self._hydrate(result.hits), start=1):
            sources = "+".join(hit.sources) if hit.sources else "unknown"
            lines.append(
                f"{position}. [{sources}] {hit.title or 'untitled'} (chunk {hit.idx})"
            )
            lines.append(f"   {self._cap_content(hit.content)}")
        if result.used_expansion:
            lines.append("note: search query was expanded with additional terms.")
        if result.note:
            lines.append(f"note: {result.note}")
        return truncate("\n".join(lines), self._max_output_chars)

    def _hydrate(self, hits: list[FusedHit]) -> list[FusedHit]:
        """Join store metadata onto hits with empty content; keep the rest.

        Empty content marks a metadata-less hit (VectorIndex stubs that
        missed the in-process cache, e.g. after a restart). Metadata comes
        from one batched ``metadata_by_ids`` read; hits still unknown to
        the store pass through unchanged. Fused score and sources are
        never touched.
        """
        if self._store is None:
            return hits
        missing = [hit.chunk_id for hit in hits if not hit.content]
        if not missing:
            return hits
        found = self._store.metadata_by_ids(missing)
        if not found:
            return hits
        hydrated: list[FusedHit] = []
        for hit in hits:
            meta = found.get(hit.chunk_id) if not hit.content else None
            if meta is None:
                hydrated.append(hit)
                continue
            hydrated.append(
                replace(
                    hit,
                    doc_id=meta.doc_id,
                    title=meta.title,
                    idx=meta.idx,
                    content=meta.content,
                )
            )
        return hydrated

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

    ``bot.build_kb_stack`` composes it and ``bot.build_tool_registry``
    turns it into agent tools. ``metadata`` is the shared chunk_id →
    hydrated-hit cache (score is a placeholder): :class:`KbIngestTool`
    populates it on ingest and :class:`VectorIndexAdapter` consults it on
    search. ``ingest_lock`` serializes the complete
    write/snapshot/publish sequence of every :class:`KbIngestTool` wired
    from this stack — the bot constructs fresh tools per upload, so the
    lock must live on the stack, not on a tool instance. ``close()``
    releases the store and embedder resources best-effort (the expansion
    LLM, when configured, owns its client).
    """

    cfg: RetrievalConfig
    store: ChunkStore
    embedder: Embedder
    vector_index: VectorIndex
    vector: VectorSearch
    retriever: HybridRetriever
    metadata: dict[str, ChunkHit] = field(default_factory=dict)
    fts: FtsSearch | None = None
    expander: QueryExpander | None = None
    ingest_lock: threading.Lock = field(default_factory=threading.Lock)

    def close(self) -> None:
        """Best-effort release of the store and embedder resources."""
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

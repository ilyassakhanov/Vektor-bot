"""SQLite-backed chunk store — the only SQL boundary of the knowledge base.

ChunkStore persists chunks in a `chunks` table and mirrors their content into
a standalone FTS5 virtual table keyed by chunk id, keeping keyword search
mappable back to full chunk metadata. Both tables are written in a single
transaction, so storage and the full-text index can never drift apart.

Stable chunk ids come from chunk_id_for — sha256 over ``doc_id + ":" + idx`` —
so re-ingesting the same chunk of the same document overwrites rows instead
of duplicating them.

search_fts scores are ``-bm25()`` (higher is better, best-first ordering).
They are a full-text-only ranking signal: rrf_fuse ignores ChunkHit.score, so
BM25 values are never mixed with vector similarity scores.

FTS5 availability is probed once at init. When the SQLite build lacks FTS5
the store logs a warning and continues in vector-only mode: chunks (and
embedding BLOBs) are still stored, search_fts returns an empty list.

The connection is opened with ``check_same_thread=False`` because the hybrid
retriever runs FTS searches in ThreadPoolExecutor worker threads; a module
lock serializes every access, which also keeps write transactions atomic
relative to reads.

metadata_by_ids is the batched metadata read for restart-time hydration:
writers refresh an in-process chunk metadata cache on ingest, but a fresh
process starts with an empty cache while vectors persist — callers join
full metadata (doc_id/title/idx/content, score 0.0) back onto hits by
chunk id, in batches below SQLite's host parameter limit.

A small ``meta`` key/value table persists knowledge-base-level facts next
to the data they describe (embedding model name and vector dimension).
Callers validate those against the current configuration and raise
:class:`KBModelError` on conflict — refusing the write or the startup is
always safer than mixing vectors from incompatible models.

``replace_chunks`` is the document-level write: it removes every stored
chunk of one ``doc_id`` (both tables) before inserting the new records, in
a single transaction, so re-ingesting with a different chunking
configuration can never leave obsolete tail chunks behind. An optional
``meta`` mapping is persisted in the same transaction — knowledge-base-level
facts (e.g. the embedding dimension) are committed atomically with the
vectors they describe, never in a second, separately-crashable write.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from retrieval.rrf import ChunkHit

log = logging.getLogger("vektor.retrieval.store")

_CREATE_CHUNKS = """
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    title TEXT NOT NULL,
    idx INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding BLOB
)
"""

_CREATE_CHUNKS_DOC_ID_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks (doc_id)"
)

_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5 (
    content,
    chunk_id UNINDEXED
)
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

META_EMBED_MODEL = "embed_model"
META_EMBED_DIM = "embed_dim"

_METADATA_BATCH = 900


class KBModelError(Exception):
    """Persisted KB metadata conflicts with the current configuration.

    Raised when the embedding model (or vector dimension) recorded in the
    store's ``meta`` table does not match what is configured now. The safe
    resolutions are restoring the previous configuration or deleting the
    database file to rebuild the knowledge base — never mixing vectors from
    incompatible models.
    """


def chunk_id_for(doc_id: str, idx: int) -> str:
    """Return the stable chunk id: sha256 hex digest of ``doc_id + ':' + idx``."""
    return hashlib.sha256(f"{doc_id}:{idx}".encode()).hexdigest()


@dataclass(frozen=True)
class ChunkRecord:
    """A single persisted chunk: document identity, position, text, embedding."""

    doc_id: str
    title: str
    idx: int
    content: str
    embedding: bytes


def _fts_match_query(terms: list[str]) -> str:
    """Build one FTS5 MATCH expression with OR semantics across terms.

    Each term becomes a quoted phrase (quotes inside a term are stripped) so
    user text is never interpreted as FTS query syntax. Returns an empty
    string when no usable term remains.
    """
    cleaned = [term for term in (t.strip().replace('"', " ") for t in terms) if term]
    if not cleaned:
        return ""
    return " OR ".join(f'"{term}"' for term in cleaned)


class ChunkStore:
    """Chunk persistence + FTS5 keyword search over one SQLite database file."""

    def __init__(self, path: str | Path) -> None:
        """Open (or create) the database at ``path`` and initialize the schema.

        The parent directory is not created — callers compose paths. FTS5 is
        probed by creating the virtual table; when unavailable, the store
        degrades to vector-only mode with ``fts_available`` set to False.
        """
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self.fts_available: bool = False
        try:
            with self._conn:
                self._conn.execute(_CREATE_CHUNKS)
                self._conn.execute(_CREATE_CHUNKS_DOC_ID_INDEX)
                self._conn.execute(_CREATE_META)
            self._create_fts_table()
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                log.warning(
                    "FTS5 is unavailable (%s); ChunkStore runs in vector-only mode", exc
                )
                return
            self._conn.close()
            raise
        self.fts_available = True

    def _create_fts_table(self) -> None:
        """Create the FTS5 virtual table; doubles as the capability probe."""
        with self._conn:
            self._conn.execute(_CREATE_FTS)

    def _insert_chunk(self, chunk: ChunkRecord) -> str:
        """Insert one chunk into both tables inside the caller's transaction."""
        chunk_id = chunk_id_for(chunk.doc_id, chunk.idx)
        self._conn.execute(
            "INSERT OR REPLACE INTO chunks"
            " (id, doc_id, title, idx, content, embedding)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                chunk_id,
                chunk.doc_id,
                chunk.title,
                chunk.idx,
                chunk.content,
                chunk.embedding,
            ),
        )
        if self.fts_available:
            self._conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
            self._conn.execute(
                "INSERT INTO chunks_fts (content, chunk_id) VALUES (?, ?)",
                (chunk.content, chunk_id),
            )
        return chunk_id

    def add_chunks(self, chunks: list[ChunkRecord]) -> int:
        """Upsert chunks into both tables in one transaction; return the count.

        Existing rows with the same stable id are replaced in `chunks` and
        re-inserted in `chunks_fts` (delete-then-insert), so re-ingesting a
        document overwrites its chunks instead of duplicating them. When FTS5
        is unavailable only the `chunks` rows are written.
        """
        if not chunks:
            return 0
        with self._lock, self._conn:
            for chunk in chunks:
                self._insert_chunk(chunk)
        return len(chunks)

    def replace_chunks(
        self,
        doc_id: str,
        chunks: list[ChunkRecord],
        meta: dict[str, str] | None = None,
    ) -> int:
        """Atomically replace all stored chunks of ``doc_id`` with ``chunks``.

        Deletes every existing row for the document from both `chunks` and
        `chunks_fts` — including tail chunks left over from a previous
        chunking configuration — then inserts the new records, all in one
        transaction. Unlike :meth:`add_chunks` this can never leave obsolete
        content of the same document behind. When ``meta`` is given, its
        key/value pairs are upserted into the meta table inside the same
        transaction, so the chunks and the facts describing them (e.g. the
        embedding dimension) commit atomically. An empty ``chunks`` list is
        a no-op (it never deletes the stored document, never writes meta).
        When FTS5 is unavailable only the `chunks` rows are replaced.
        """
        if not chunks:
            return 0
        with self._lock, self._conn:
            if self.fts_available:
                self._conn.execute(
                    "DELETE FROM chunks_fts WHERE chunk_id IN"
                    " (SELECT id FROM chunks WHERE doc_id = ?)",
                    (doc_id,),
                )
            self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            for chunk in chunks:
                self._insert_chunk(chunk)
            for key, value in (meta or {}).items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
        return len(chunks)

    def get_meta(self, key: str) -> str | None:
        """Return the stored value for ``key``, or None when unset."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        """Persist ``key = value`` in the meta table (insert or replace)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )

    def search_fts(self, terms: list[str], limit: int) -> list[ChunkHit]:
        """Full-text search: BM25-ordered ChunkHits, best first.

        Terms are combined with OR semantics; a chunk matches when it contains
        any of them. Each hit's score is ``-bm25()`` (higher is better) — an
        FTS-only signal that rrf_fuse deliberately ignores. Equal scores
        tie-break by ascending chunk_id so ordering (and LIMIT selection) is
        deterministic regardless of SQLite internals. Returns an empty
        list when FTS5 is unavailable or no term is usable.
        """
        if not self.fts_available:
            return []
        match = _fts_match_query(terms)
        if not match:
            return []
        query = (
            "SELECT c.id, c.doc_id, c.title, c.idx, c.content, s.score"
            " FROM (SELECT chunk_id, bm25(chunks_fts) AS score FROM chunks_fts"
            " WHERE chunks_fts MATCH ?"
            " ORDER BY bm25(chunks_fts), chunk_id LIMIT ?) AS s"
            " JOIN chunks AS c ON c.id = s.chunk_id"
            " ORDER BY s.score, s.chunk_id"
        )
        with self._lock:
            rows = self._conn.execute(query, (match, limit)).fetchall()
        return [
            ChunkHit(
                chunk_id=row[0],
                doc_id=row[1],
                title=row[2],
                idx=row[3],
                content=row[4],
                score=-row[5],
            )
            for row in rows
        ]

    def all_vectors(self) -> list[tuple[str, bytes]]:
        """Return (chunk_id, embedding BLOB) pairs for building the vector index."""
        with self._lock:
            rows = self._conn.execute("SELECT id, embedding FROM chunks").fetchall()
        return [(row[0], bytes(row[1])) for row in rows]

    def metadata_by_ids(self, chunk_ids: list[str]) -> dict[str, ChunkHit]:
        """Return chunk metadata keyed by chunk id; unknown ids are omitted.

        A lock-guarded batched SELECT of id/doc_id/title/idx/content for
        restart-time hydration: callers that reloaded vectors from this
        store in a fresh process join the returned :class:`ChunkHit`\\s
        (score is the 0.0 placeholder — ranking lives elsewhere) back onto
        their hits. Ids are queried in batches of 900, below SQLite's
        conservative host parameter limit. Empty input returns an empty
        dict without touching the database.
        """
        found: dict[str, ChunkHit] = {}
        if not chunk_ids:
            return found
        with self._lock:
            for start in range(0, len(chunk_ids), _METADATA_BATCH):
                batch = chunk_ids[start : start + _METADATA_BATCH]
                placeholders = ",".join("?" * len(batch))
                rows = self._conn.execute(
                    "SELECT id, doc_id, title, idx, content FROM chunks"
                    f" WHERE id IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    found[row[0]] = ChunkHit(
                        chunk_id=row[0],
                        doc_id=row[1],
                        title=row[2],
                        idx=row[3],
                        content=row[4],
                        score=0.0,
                    )
        return found

    def count(self) -> int:
        """Return the number of stored chunks."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return int(row[0])

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

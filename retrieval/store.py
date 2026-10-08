"""SQLite-backed KB store — documents, chunks, FTS5, and sqlite-vec in one file.

Writes are single-transaction across all four tables; every search takes a
mandatory ``user_id`` (SQL-level isolation); L2-normalized float32 vectors
(L2 distance ranks like cosine); stable sha256 ids → re-ingest is an upsert.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import numpy as np

from retrieval.rrf import ChunkHit
from retrieval.vec import load as _load_vec

log = logging.getLogger("vektor.retrieval.store")

_CREATE_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_CHUNKS = """
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    page INTEGER,
    embedding BLOB
)
"""

_CREATE_CHUNKS_DOCUMENT_ID_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks (document_id)"
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

_VEC_TABLE = "vec_chunks"

META_EMBED_MODEL = "embed_model"
META_EMBED_DIM = "embed_dim"

_METADATA_BATCH = 900

_KNN_SQL = """
SELECT v.chunk_id, c.document_id, d.filename, c.chunk_index, c.text, c.page, v.distance
FROM vec_chunks AS v
JOIN chunks AS c ON c.id = v.chunk_id
JOIN documents AS d ON d.id = c.document_id
WHERE v.owner = ? AND v.embedding MATCH ? AND k = ?
"""

_FTS_SQL = """
SELECT c.id, c.document_id, d.filename, c.chunk_index, c.text, c.page,
       bm25(chunks_fts) AS score
FROM chunks_fts
JOIN chunks AS c ON c.id = chunks_fts.chunk_id
JOIN documents AS d ON d.id = c.document_id
WHERE chunks_fts MATCH ? AND d.user_id = ?
ORDER BY score, c.id
LIMIT ?
"""


class KBModelError(Exception):
    """Persisted KB state conflicts with the current code or configuration.

    Stored meta (embedding model/dimension) mismatches the config, or the
    DB file uses the legacy pre-documents schema; delete the DB file.
    """


def chunk_id_for(doc_id: str, idx: int) -> str:
    """Return the stable chunk id: sha256 hex digest of ``doc_id + ':' + idx``."""
    return hashlib.sha256(f"{doc_id}:{idx}".encode()).hexdigest()


def to_blob(vector: list[float] | np.ndarray) -> bytes:
    """Serialize ``vector`` as an L2-normalized float32 BLOB for vec0.

    A zero vector stays all-zeros (never NaN downstream); raises ValueError
    on non-finite components (e.g. float32 overflow after the cast).
    """
    array = np.asarray(vector, dtype=np.float32).ravel()
    if not np.isfinite(array).all():
        raise ValueError(
            "vector component overflows the float32 range (inf/nan after cast)"
        )
    norm = float(np.linalg.norm(array))
    if norm > 0.0:
        array = array / norm
        if not np.isfinite(array).all():
            raise ValueError("vector normalization produced a non-finite vector")
    return array.tobytes()


@dataclass(frozen=True)
class ChunkRecord:
    """A single persisted chunk: document identity, position, text, embedding."""

    document_id: str
    chunk_index: int
    text: str
    embedding: bytes
    page: int | None = None


@dataclass(frozen=True)
class DocumentRecord:
    """A document plus its chunks — the unit of the add_document transaction."""

    id: str
    user_id: str
    filename: str
    file_type: str
    created_at: str
    chunks: list[ChunkRecord]


@dataclass(frozen=True)
class DocumentRow:
    """One entry of list_documents: document identity plus its chunk count."""

    id: str
    filename: str
    created_at: str
    chunk_count: int


def _fts_match_query(terms: list[str]) -> str:
    """Build one FTS5 MATCH expression with OR semantics across terms.

    Each term becomes a quoted phrase (never FTS query syntax); empty when
    no usable term remains.
    """
    cleaned = [term for term in (t.strip().replace('"', " ") for t in terms) if term]
    if not cleaned:
        return ""
    return " OR ".join(f'"{term}"' for term in cleaned)


class ChunkStore:
    """Document/chunk persistence + vec0 KNN + FTS5 search over one SQLite file."""

    def __init__(self, path: str | Path) -> None:
        """Open (or create) the database at ``path`` and initialize the schema.

        Parent dir not created; ``vec_chunks`` lazy; legacy schema raises
        :class:`KBModelError`; FTS5 probe failure degrades to vector-only.
        """
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self.fts_available: bool = False
        _load_vec(self._conn)
        if self._is_legacy_schema():
            self._conn.close()
            raise KBModelError(
                f"Knowledge base at {path} uses the legacy schema (no"
                " documents table). It is incompatible with the current"
                " version — delete the database file to rebuild the"
                " knowledge base."
            )
        with self._lock, self._conn:
            self._conn.execute(_CREATE_DOCUMENTS)
            self._conn.execute(_CREATE_CHUNKS)
            self._conn.execute(_CREATE_CHUNKS_DOCUMENT_ID_INDEX)
            self._conn.execute(_CREATE_META)
        try:
            fts_existed = self._fts_table_exists()
            with self._lock, self._conn:
                self._conn.execute("BEGIN")
                self._create_fts_table()
                if not fts_existed:
                    self._backfill_fts()
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                log.warning(
                    "FTS5 is unavailable (%s); ChunkStore runs in vector-only mode", exc
                )
                return
            self._conn.close()
            raise
        self.fts_available = True

    def _is_legacy_schema(self) -> bool:
        """Return True when a ``chunks`` table predates the documents schema."""
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks'"
        ).fetchone()
        if row is None:
            return False
        columns = {info[1] for info in self._conn.execute("PRAGMA table_info(chunks)")}
        return "document_id" not in columns

    def _fts_table_exists(self) -> bool:
        """Return True when ``chunks_fts`` already exists in the database."""
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks_fts'"
        ).fetchone()
        return row is not None

    def _vec_table_exists(self) -> bool:
        """Return True when ``vec_chunks`` already exists in the database."""
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_VEC_TABLE,),
        ).fetchone()
        return row is not None

    def _create_fts_table(self) -> None:
        """Create the FTS5 virtual table; doubles as the capability probe."""
        self._conn.execute(_CREATE_FTS)

    def _backfill_fts(self) -> None:
        """Populate the freshly created ``chunks_fts`` from ``chunks``."""
        cursor = self._conn.execute(
            "INSERT INTO chunks_fts (content, chunk_id) SELECT text, id FROM chunks"
        )
        backfilled = cursor.rowcount
        if backfilled:
            log.info(
                "FTS5 became available; backfilled %d stored chunks into chunks_fts",
                backfilled,
            )

    def _ensure_vec_table(self, dim: int) -> None:
        """Create ``vec_chunks`` for ``dim`` (once) inside the caller's txn."""
        if self._vec_table_exists():
            return
        self._conn.execute(
            f"CREATE VIRTUAL TABLE {_VEC_TABLE} USING vec0("
            f"chunk_id TEXT PRIMARY KEY, owner TEXT, embedding FLOAT[{dim}])"
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (META_EMBED_DIM, str(dim)),
        )

    def _delete_document_rows(self, doc_ids: list[str]) -> None:
        """Delete all chunk/FTS/vec rows of ``doc_ids`` inside the caller's txn."""
        placeholders = ",".join("?" * len(doc_ids))
        if self.fts_available:
            self._conn.execute(
                "DELETE FROM chunks_fts WHERE chunk_id IN"
                f" (SELECT id FROM chunks WHERE document_id IN ({placeholders}))",
                doc_ids,
            )
        if self._vec_table_exists():
            self._conn.execute(
                f"DELETE FROM {_VEC_TABLE} WHERE chunk_id IN"
                f" (SELECT id FROM chunks WHERE document_id IN ({placeholders}))",
                doc_ids,
            )
        self._conn.execute(
            f"DELETE FROM chunks WHERE document_id IN ({placeholders})", doc_ids
        )

    def add_document(self, doc: DocumentRecord) -> None:
        """Write the documents row, chunks, FTS mirror, and vec0 rows atomically.

        Re-ingesting a document id replaces its chunks (upsert, reowned by
        ``doc.user_id``); an empty chunk list is a no-op.
        """
        if not doc.chunks:
            return
        dim = len(doc.chunks[0].embedding) // 4
        with self._lock, self._conn:
            self._conn.execute("BEGIN")
            self._ensure_vec_table(dim)
            self._delete_document_rows([doc.id])
            self._conn.execute(
                "INSERT OR REPLACE INTO documents"
                " (id, user_id, filename, file_type, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (doc.id, doc.user_id, doc.filename, doc.file_type, doc.created_at),
            )
            for chunk in doc.chunks:
                chunk_id = chunk_id_for(chunk.document_id, chunk.chunk_index)
                self._conn.execute(
                    "INSERT INTO chunks"
                    " (id, document_id, chunk_index, text, page, embedding)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        chunk.document_id,
                        chunk.chunk_index,
                        chunk.text,
                        chunk.page,
                        chunk.embedding,
                    ),
                )
                if self.fts_available:
                    self._conn.execute(
                        "INSERT INTO chunks_fts (content, chunk_id) VALUES (?, ?)",
                        (chunk.text, chunk_id),
                    )
                self._conn.execute(
                    f"INSERT INTO {_VEC_TABLE} (chunk_id, owner, embedding)"
                    " VALUES (?, ?, ?)",
                    (chunk_id, doc.user_id, chunk.embedding),
                )

    def list_documents(self, user_id: str) -> list[DocumentRow]:
        """Return the user's documents (newest first) with chunk counts."""
        rows = self._read_all(
            """
            SELECT d.id, d.filename, d.created_at, COUNT(c.id)
            FROM documents AS d
            LEFT JOIN chunks AS c ON c.document_id = d.id
            WHERE d.user_id = ?
            GROUP BY d.id, d.filename, d.created_at
            ORDER BY d.created_at DESC, d.filename ASC
            """,
            (user_id,),
        )
        return [
            DocumentRow(
                id=row[0], filename=row[1], created_at=row[2], chunk_count=int(row[3])
            )
            for row in rows
        ]

    def delete_document(self, user_id: str, filename: str) -> bool:
        """Delete all documents of ``user_id`` named ``filename``; cascade all rows.

        One transaction over documents + chunks + FTS + vec0; returns False
        (touching nothing) when no such document belongs to the user.
        """
        with self._lock, self._conn:
            self._conn.execute("BEGIN")
            owned = [
                row[0]
                for row in self._conn.execute(
                    "SELECT id FROM documents WHERE user_id = ? AND filename = ?",
                    (user_id, filename),
                ).fetchall()
            ]
            if not owned:
                self._conn.rollback()
                return False
            self._delete_document_rows(owned)
            placeholders = ",".join("?" * len(owned))
            self._conn.execute(
                f"DELETE FROM documents WHERE id IN ({placeholders})", owned
            )
        return True

    def search_vec(
        self, user_id: str, queries: list[list[float]], limit: int
    ) -> list[ChunkHit]:
        """KNN search over the user's vectors; best (smallest-distance) first.

        One KNN query per query vector; a chunk's distance is its minimum
        across queries; ties break by chunk_id; [] on empty/degenerate input.
        """
        if limit <= 0 or not queries or not self._vec_table_exists():
            return []
        merged: dict[str, tuple[float, tuple[Any, ...]]] = {}
        with self._lock:
            for query in queries:
                blob = to_blob(query)
                rows = self._conn.execute(_KNN_SQL, (user_id, blob, limit)).fetchall()
                for row in rows:
                    known = merged.get(row[0])
                    if known is None or row[6] < known[0]:
                        merged[row[0]] = (row[6], row)
        ranked = sorted(merged.values(), key=lambda pair: (pair[0], pair[1][0]))
        return [
            ChunkHit(
                chunk_id=row[0],
                doc_id=row[1],
                title=row[2],
                idx=row[3],
                content=row[4],
                score=float(row[6]),
                page=row[5],
            )
            for _distance, row in ranked[:limit]
        ]

    def search_fts(self, user_id: str, terms: list[str], limit: int) -> list[ChunkHit]:
        """Owner-filtered full-text search: BM25-ordered ChunkHits, best first.

        Terms combine with OR semantics; score is ``-bm25()``; ties break
        by chunk_id; [] when FTS5 is unavailable or no term is usable.
        """
        if not self.fts_available:
            return []
        match = _fts_match_query(terms)
        if not match:
            return []
        rows = self._read_all(_FTS_SQL, (match, user_id, limit))
        return [
            ChunkHit(
                chunk_id=row[0],
                doc_id=row[1],
                title=row[2],
                idx=row[3],
                content=row[4],
                score=-row[6],
                page=row[5],
            )
            for row in rows
        ]

    def metadata_by_ids(self, chunk_ids: list[str]) -> dict[str, ChunkHit]:
        """Return chunk metadata (title = document filename) keyed by chunk id.

        Lock-guarded batched SELECT (batches of 900); unknown ids omitted.
        """
        found: dict[str, ChunkHit] = {}
        if not chunk_ids:
            return found
        with self._lock:
            for start in range(0, len(chunk_ids), _METADATA_BATCH):
                batch = chunk_ids[start : start + _METADATA_BATCH]
                placeholders = ",".join("?" * len(batch))
                rows = self._conn.execute(
                    "SELECT c.id, c.document_id, d.filename, c.chunk_index, c.text,"
                    " c.page"
                    " FROM chunks AS c"
                    " JOIN documents AS d ON d.id = c.document_id"
                    f" WHERE c.id IN ({placeholders})",
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
                        page=row[5],
                    )
        return found

    def _read_all(self, query: str, params: tuple[object, ...] = ()) -> list[tuple]:
        """Run one lock-guarded SELECT and fetch every row."""
        with self._lock:
            return self._conn.execute(query, params).fetchall()

    def get_meta(self, key: str) -> str | None:
        """Return the stored value for ``key``, or None when unset."""
        rows = self._read_all("SELECT value FROM meta WHERE key = ?", (key,))
        return None if not rows else str(rows[0][0])

    def set_meta(self, key: str, value: str) -> None:
        """Persist ``key = value`` in the meta table (insert or replace)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )

    def count(self) -> int:
        """Return the number of stored chunks (all users)."""
        rows = self._read_all("SELECT COUNT(*) FROM chunks")
        return int(rows[0][0])

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

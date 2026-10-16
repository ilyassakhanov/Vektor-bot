"""Tests for ChunkStore — SQLite documents/chunks + FTS5 + sqlite-vec storage.

Writes are single-transaction (all four tables), reads owner-filtered; covers
atomicity, search_vec/fts, lazy vec0, upsert, delete cascade, meta, to_blob.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from retrieval.rrf import ChunkHit
from retrieval.store import (
    META_EMBED_DIM,
    META_EMBED_MODEL,
    ChunkRecord,
    ChunkStore,
    DocumentRecord,
    DocumentRow,
    KBModelError,
    chunk_id_for,
    to_blob,
)


def _blob(*components: float) -> bytes:
    """Serialize components as a normalized float32 embedding BLOB."""
    return to_blob(list(components))


def _chunk(
    doc_id: str,
    idx: int,
    text: str,
    embedding: bytes | None = None,
    page: int | None = None,
) -> ChunkRecord:
    return ChunkRecord(
        document_id=doc_id,
        chunk_index=idx,
        text=text,
        embedding=embedding if embedding is not None else _blob(1.0, 0.0, 0.0, 0.0),
        page=page,
    )


def _doc(
    doc_id: str,
    chunks: list[ChunkRecord],
    user_id: str = "u1",
    filename: str = "Doc title",
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> DocumentRecord:
    return DocumentRecord(
        id=doc_id,
        user_id=user_id,
        filename=filename,
        file_type="txt",
        created_at=created_at,
        chunks=chunks,
    )


class TestChunkIdFor:
    def test_same_inputs_same_id(self) -> None:
        assert chunk_id_for("doc-1", 0) == chunk_id_for("doc-1", 0)

    def test_different_idx_different_id(self) -> None:
        assert chunk_id_for("doc-1", 0) != chunk_id_for("doc-1", 1)

    def test_different_doc_different_id(self) -> None:
        assert chunk_id_for("doc-1", 0) != chunk_id_for("doc-2", 0)

    def test_matches_sha256_of_doc_id_colon_idx(self) -> None:
        assert chunk_id_for("doc-1", 7) == hashlib.sha256(b"doc-1:7").hexdigest()


class TestToBlob:
    def test_normalizes_to_unit_l2_norm(self) -> None:
        decoded = np.frombuffer(to_blob([3.0, 0.0, 4.0]), dtype=np.float32)
        assert float(np.linalg.norm(decoded)) == pytest.approx(1.0, rel=1e-6)
        assert [float(x) for x in decoded] == pytest.approx([0.6, 0.0, 0.8])

    def test_unit_vectors_unchanged(self) -> None:
        decoded = np.frombuffer(to_blob([1.5, -2.0, 0.25]), dtype=np.float32)
        norm = float(np.linalg.norm([1.5, -2.0, 0.25]))
        assert [float(x) for x in decoded] == pytest.approx(
            [1.5 / norm, -2.0 / norm, 0.25 / norm]
        )

    def test_zero_vector_stays_zero(self) -> None:
        decoded = np.frombuffer(to_blob([0.0, 0.0]), dtype=np.float32)
        assert list(decoded) == [0.0, 0.0]

    def test_produces_float32_bytes(self) -> None:
        blob = to_blob([1.0, 2.0])
        assert len(blob) == 8
        assert np.frombuffer(blob, dtype=np.float32).dtype == np.float32

    def test_rejects_non_finite_component(self) -> None:
        with pytest.raises(ValueError, match="float32"):
            to_blob([float("nan"), 1.0])

    def test_rejects_float32_overflow_component(self) -> None:
        with pytest.raises(ValueError, match="float32"):
            to_blob([1e39])


class TestAddDocument:
    def test_writes_documents_chunks_fts_and_vec(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "doc-1",
                    [
                        _chunk("doc-1", 0, "postgres replication intro"),
                        _chunk("doc-1", 1, "failover and WAL"),
                    ],
                )
            )

            assert store.count() == 2
            docs = store.list_documents("u1")
            assert [d.filename for d in docs] == ["Doc title"]
            assert docs[0].chunk_count == 2

            fts_hits = store.search_fts("u1", ["replication"], limit=10)
            assert [h.chunk_id for h in fts_hits] == [chunk_id_for("doc-1", 0)]
            assert fts_hits[0].doc_id == "doc-1"
            assert fts_hits[0].title == "Doc title"
            assert fts_hits[0].idx == 0
            assert fts_hits[0].content == "postgres replication intro"

            vec_hits = store.search_vec("u1", [[1.0, 0.0, 0.0, 0.0]], limit=10)
            assert vec_hits[0].chunk_id == chunk_id_for("doc-1", 0)
            assert vec_hits[0].content == "postgres replication intro"
        finally:
            store.close()

    def test_page_column_persisted(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "doc-1",
                    [
                        _chunk("doc-1", 0, "paged content", page=3),
                        _chunk("doc-1", 1, "unpaged content"),
                    ],
                )
            )
            page_0 = store._conn.execute(
                "SELECT page FROM chunks WHERE id = ?", (chunk_id_for("doc-1", 0),)
            ).fetchone()[0]
            page_1 = store._conn.execute(
                "SELECT page FROM chunks WHERE id = ?", (chunk_id_for("doc-1", 1),)
            ).fetchone()[0]
            assert page_0 == 3
            assert page_1 is None
        finally:
            store.close()

    def test_empty_chunks_is_noop(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(_doc("doc-1", []))
            assert store.count() == 0
            assert store.list_documents("u1") == []
        finally:
            store.close()

    def test_failure_rolls_back_whole_transaction(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc("doc-1", [_chunk("doc-1", 0, "kept")], filename="A")
            )

            bad = ChunkRecord(
                document_id="doc-2",
                chunk_index=0,
                text="bad dim",
                embedding=_blob(1.0, 0.0, 0.0, 0.0, 0.0),
            )
            with pytest.raises(sqlite3.OperationalError):
                store.add_document(_doc("doc-2", [bad], filename="B"))

            assert store.count() == 1
            assert [d.filename for d in store.list_documents("u1")] == ["A"]
            assert store.search_fts("u1", ["kept"], limit=10)
            assert store.search_fts("u1", ["bad"], limit=10) == []
        finally:
            store.close()

    def test_mixed_dims_within_one_document_roll_back(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            mixed = [
                _chunk("doc-1", 0, "good"),
                ChunkRecord(
                    document_id="doc-1",
                    chunk_index=1,
                    text="bad",
                    embedding=_blob(1.0, 2.0, 3.0),
                ),
            ]
            with pytest.raises(sqlite3.OperationalError):
                store.add_document(_doc("doc-1", mixed))

            assert store.count() == 0
            assert store.list_documents("u1") == []
            assert store.search_fts("u1", ["good"], limit=10) == []
        finally:
            store.close()


class TestSearchVec:
    def test_l2_ordering_on_normalized_vectors(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "d",
                    [
                        _chunk("d", 0, "aligned", embedding=_blob(5.0, 0.0, 0.0, 0.0)),
                        _chunk("d", 1, "oblique", embedding=_blob(1.0, 1.0, 0.0, 0.0)),
                        _chunk(
                            "d", 2, "orthogonal", embedding=_blob(0.0, 0.0, 9.0, 0.0)
                        ),
                        _chunk(
                            "d", 3, "opposite", embedding=_blob(-2.0, 0.0, 0.0, 0.0)
                        ),
                    ],
                )
            )

            hits = store.search_vec("u1", [[1.0, 0.0, 0.0, 0.0]], limit=10)

            assert [h.idx for h in hits] == [0, 1, 2, 3]
            assert hits[0].score == pytest.approx(0.0, abs=1e-6)
            assert hits[1].score == pytest.approx(
                float(np.sqrt(2.0 - np.sqrt(2.0))), rel=1e-5
            )
            assert hits[2].score == pytest.approx(float(np.sqrt(2.0)), rel=1e-5)
            assert hits[3].score == pytest.approx(2.0, rel=1e-5)
        finally:
            store.close()

    def test_magnitudes_do_not_matter(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "d",
                    [
                        _chunk("d", 0, "tiny", embedding=_blob(0.001, 0.0, 0.0, 0.0)),
                        _chunk("d", 1, "huge", embedding=_blob(1000.0, 0.0, 0.0, 0.0)),
                    ],
                )
            )

            hits = store.search_vec("u1", [[3.0, 0.0, 0.0, 0.0]], limit=2)

            assert hits[0].score == pytest.approx(hits[1].score, abs=1e-6)
            assert not np.isnan(hits[0].score)
        finally:
            store.close()

    def test_stored_zero_vector_scores_finite_not_nan(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "d",
                    [
                        _chunk("d", 0, "pos", embedding=_blob(1.0, 0.0, 0.0, 0.0)),
                        _chunk("d", 1, "zero", embedding=_blob(0.0, 0.0, 0.0, 0.0)),
                    ],
                )
            )

            hits = store.search_vec("u1", [[1.0, 0.0, 0.0, 0.0]], limit=2)

            assert [h.idx for h in hits] == [0, 1]
            assert hits[1].score == pytest.approx(1.0)
            assert not np.isnan(hits[1].score)
        finally:
            store.close()

    def test_zero_query_scores_norm_of_each_vector(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "d",
                    [
                        _chunk("d", 0, "pos", embedding=_blob(1.0, 0.0, 0.0, 0.0)),
                        _chunk("d", 1, "zero", embedding=_blob(0.0, 0.0, 0.0, 0.0)),
                    ],
                )
            )

            hits = store.search_vec("u1", [[0.0, 0.0, 0.0, 0.0]], limit=2)

            assert [h.idx for h in hits] == [1, 0]
            assert hits[0].score == pytest.approx(0.0, abs=1e-7)
            assert hits[1].score == pytest.approx(1.0)
            assert not any(np.isnan(h.score) for h in hits)
        finally:
            store.close()

    def test_multi_query_min_distance_merge(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "d",
                    [
                        ChunkRecord(
                            document_id="d",
                            chunk_index=0,
                            text="med",
                            embedding=_blob(0.8, 0.6),
                        ),
                        ChunkRecord(
                            document_id="d",
                            chunk_index=1,
                            text="best2",
                            embedding=_blob(0.0, 1.0),
                        ),
                        ChunkRecord(
                            document_id="d",
                            chunk_index=2,
                            text="best1",
                            embedding=_blob(1.0, 0.0),
                        ),
                    ],
                )
            )

            hits = store.search_vec("u1", [[1.0, 0.0], [0.0, 1.0]], limit=3)

            assert {hits[0].idx, hits[1].idx} == {1, 2}
            assert [hits[0].chunk_id, hits[1].chunk_id] == sorted(
                [hits[0].chunk_id, hits[1].chunk_id]
            )
            assert hits[0].score == pytest.approx(0.0, abs=1e-7)
            assert hits[1].score == pytest.approx(0.0, abs=1e-7)
            assert hits[2].idx == 0
            assert hits[2].score == pytest.approx(float(np.sqrt(0.4)), rel=1e-5)
        finally:
            store.close()

    def test_ties_break_by_chunk_id(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "d",
                    [
                        _chunk("d", 0, "same", embedding=_blob(1.0, 2.0)),
                        _chunk("d", 1, "same", embedding=_blob(1.0, 2.0)),
                    ],
                )
            )

            hits = store.search_vec("u1", [[1.0, 2.0]], limit=2)

            assert [h.chunk_id for h in hits] == sorted(
                [chunk_id_for("d", 0), chunk_id_for("d", 1)]
            )
            assert hits[0].score == pytest.approx(0.0, abs=1e-6)
        finally:
            store.close()

    def test_limit_respected(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "d",
                    [
                        _chunk("d", i, f"c{i}", embedding=_blob(1.0, float(i)))
                        for i in range(4)
                    ],
                )
            )

            hits = store.search_vec("u1", [[1.0, 3.0]], limit=2)

            assert len(hits) == 2
            assert hits[0].idx == 3
            assert store.search_vec("u1", [[1.0, 3.0]], limit=0) == []
            assert store.search_vec("u1", [], limit=5) == []
        finally:
            store.close()

    def test_unknown_owner_returns_empty(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(_doc("d", [_chunk("d", 0, "content")]))

            assert store.search_vec("ghost", [[1.0, 0.0, 0.0, 0.0]], limit=5) == []
        finally:
            store.close()

    def test_empty_store_returns_empty(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            assert store.search_vec("u1", [[1.0, 0.0, 0.0, 0.0]], limit=5) == []
        finally:
            store.close()

    def test_dimension_mismatch_raises(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(_doc("d", [_chunk("d", 0, "c")]))

            with pytest.raises(sqlite3.OperationalError):
                store.search_vec("u1", [[1.0, 0.0]], limit=1)
        finally:
            store.close()


class TestLazyVecCreation:
    def test_no_vec_table_or_dim_before_first_ingest(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            row = store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'vec_chunks'"
            ).fetchone()
            assert row is None
            assert store.get_meta(META_EMBED_DIM) is None
        finally:
            store.close()

    def test_first_ingest_creates_table_and_dim_meta(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(_doc("d", [_chunk("d", 0, "content")]))

            row = store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'vec_chunks'"
            ).fetchone()
            assert row is not None
            assert store.get_meta(META_EMBED_DIM) == "4"
        finally:
            store.close()

    def test_second_ingest_reuses_existing_table(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(_doc("d1", [_chunk("d1", 0, "first")]))
            store.add_document(_doc("d2", [_chunk("d2", 0, "second")]))

            assert store.get_meta(META_EMBED_DIM) == "4"
            assert store.count() == 2
            hits = store.search_vec("u1", [[1.0, 0.0, 0.0, 0.0]], limit=10)
            assert {h.doc_id for h in hits} == {"d1", "d2"}
        finally:
            store.close()


class TestLegacyDatabase:
    def _make_legacy_db(self, path: Path) -> None:
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE chunks ("
            " id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, title TEXT NOT NULL,"
            " idx INTEGER NOT NULL, content TEXT NOT NULL, embedding BLOB)"
        )
        conn.commit()
        conn.close()

    def test_legacy_schema_raises_kb_model_error(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.db"
        self._make_legacy_db(path)

        with pytest.raises(KBModelError, match="delete"):
            ChunkStore(path)

    def test_legacy_error_closes_connection(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.db"
        self._make_legacy_db(path)
        with pytest.raises(KBModelError):
            ChunkStore(path)
        conn = sqlite3.connect(str(path))
        conn.execute("SELECT 1")
        conn.close()


class TestUpsert:
    def test_reingest_same_doc_replaces_without_duplicates(
        self, tmp_path: Path
    ) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "doc-1",
                    [
                        _chunk("doc-1", 0, "old content about postgres"),
                        _chunk("doc-1", 1, "obsolete tail"),
                    ],
                )
            )
            store.add_document(
                _doc("doc-1", [_chunk("doc-1", 0, "new content about mongodb")])
            )

            assert store.count() == 1
            assert [h.idx for h in store.search_fts("u1", ["mongodb"], limit=10)] == [0]
            assert store.search_fts("u1", ["postgres"], limit=10) == []
            assert store.search_fts("u1", ["obsolete"], limit=10) == []

            hits = store.search_vec("u1", [[1.0, 0.0, 0.0, 0.0]], limit=10)
            assert [h.chunk_id for h in hits] == [chunk_id_for("doc-1", 0)]

            blob = store._conn.execute(
                "SELECT embedding FROM chunks WHERE id = ?", (chunk_id_for("doc-1", 0),)
            ).fetchone()[0]
            assert bytes(blob) == _blob(1.0, 0.0, 0.0, 0.0)
        finally:
            store.close()

    def test_stable_chunk_ids_across_reingest(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(_doc("doc-1", [_chunk("doc-1", 0, "v1")]))
            first_id = store.search_fts("u1", ["v1"], limit=1)[0].chunk_id
            store.add_document(_doc("doc-1", [_chunk("doc-1", 0, "v2")]))

            hits = store.search_fts("u1", ["v2"], limit=1)
            assert hits[0].chunk_id == first_id == chunk_id_for("doc-1", 0)
        finally:
            store.close()

    def test_reingest_with_different_owner_reassigns_document(
        self, tmp_path: Path
    ) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(_doc("doc-1", [_chunk("doc-1", 0, "shared body")]))
            store.add_document(
                _doc(
                    "doc-1",
                    [_chunk("doc-1", 0, "shared body")],
                    user_id="u2",
                    filename="Other name",
                )
            )

            assert store.list_documents("u1") == []
            docs = store.list_documents("u2")
            assert [d.filename for d in docs] == ["Other name"]
            assert store.search_fts("u1", ["shared"], limit=10) == []
            assert store.search_fts("u2", ["shared"], limit=10)
        finally:
            store.close()

    def test_other_documents_untouched_by_upsert(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(_doc("doc-1", [_chunk("doc-1", 0, "first content")]))
            store.add_document(_doc("doc-2", [_chunk("doc-2", 0, "second content")]))

            store.add_document(_doc("doc-1", [_chunk("doc-1", 0, "first replaced")]))

            assert store.count() == 2
            assert [h.doc_id for h in store.search_fts("u1", ["second"], limit=10)] == (
                ["doc-2"]
            )
            assert [
                h.doc_id for h in store.search_fts("u1", ["content"], limit=10)
            ] == (["doc-2"])
        finally:
            store.close()


class TestDeleteDocument:
    def test_owner_delete_cascades_everywhere(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc("doc-1", [_chunk("doc-1", 0, "alpha document")], filename="Alpha")
            )
            store.add_document(
                _doc("doc-2", [_chunk("doc-2", 0, "beta document")], filename="Beta")
            )

            assert store.delete_document("u1", "Beta") is True

            assert store.count() == 1
            assert store.search_fts("u1", ["beta"], limit=10) == []
            assert store.metadata_by_ids([chunk_id_for("doc-2", 0)]) == {}
            kept = store.search_fts("u1", ["alpha"], limit=10)
            assert [h.doc_id for h in kept] == ["doc-1"]
            assert [d.filename for d in store.list_documents("u1")] == ["Alpha"]

            vec_hits = store.search_vec("u1", [[1.0, 0.0, 0.0, 0.0]], limit=10)
            assert [h.doc_id for h in vec_hits] == ["doc-1"]
        finally:
            store.close()

    def test_non_owner_delete_returns_false(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(_doc("doc-1", [_chunk("doc-1", 0, "private data")]))

            assert store.delete_document("u2", "Doc title") is False
            assert store.delete_document("u1", "missing.txt") is False
            assert store.count() == 1
            assert store.search_fts("u1", ["private"], limit=10)
        finally:
            store.close()

    def test_delete_removes_vec_rows(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc("doc-1", [_chunk("doc-1", 0, "to be removed")], filename="R")
            )
            store.add_document(
                _doc("doc-2", [_chunk("doc-2", 0, "to be kept")], filename="K")
            )
            store.delete_document("u1", "R")

            hits = store.search_vec("u1", [[1.0, 0.0, 0.0, 0.0]], limit=10)
            assert [h.doc_id for h in hits] == ["doc-2"]
        finally:
            store.close()


class TestListDocuments:
    def test_scoped_to_user_with_chunk_counts(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "d1",
                    [_chunk("d1", i, f"a{i}") for i in range(3)],
                    user_id="u1",
                    filename="a.txt",
                    created_at="2026-01-01T00:00:00+00:00",
                )
            )
            store.add_document(
                _doc(
                    "d2",
                    [_chunk("d2", 0, "b")],
                    user_id="u2",
                    filename="b.txt",
                    created_at="2026-01-02T00:00:00+00:00",
                )
            )

            mine = store.list_documents("u1")
            assert mine == [
                DocumentRow(
                    id="d1",
                    filename="a.txt",
                    created_at="2026-01-01T00:00:00+00:00",
                    chunk_count=3,
                )
            ]
            theirs = store.list_documents("u2")
            assert [d.filename for d in theirs] == ["b.txt"]
            assert theirs[0].chunk_count == 1
            assert store.list_documents("ghost") == []
        finally:
            store.close()

    def test_newest_first_ordering(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "old",
                    [_chunk("old", 0, "x")],
                    filename="old.txt",
                    created_at="2026-01-01T00:00:00+00:00",
                )
            )
            store.add_document(
                _doc(
                    "new",
                    [_chunk("new", 0, "y")],
                    filename="new.txt",
                    created_at="2026-02-01T00:00:00+00:00",
                )
            )

            docs = store.list_documents("u1")
            assert [d.filename for d in docs] == ["new.txt", "old.txt"]
        finally:
            store.close()


class TestSearchFts:
    def test_insert_syncs_chunks_and_fts(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "doc-1",
                    [
                        _chunk("doc-1", 0, "intro to postgres replication"),
                        _chunk("doc-1", 1, "failover and WAL archiving"),
                    ],
                )
            )

            hits = store.search_fts("u1", ["replication"], limit=10)
            assert len(hits) == 1
            hit = hits[0]
            assert isinstance(hit, ChunkHit)
            assert hit.chunk_id == chunk_id_for("doc-1", 0)
            assert hit.doc_id == "doc-1"
            assert hit.title == "Doc title"
            assert hit.idx == 0
            assert hit.content == "intro to postgres replication"
            assert isinstance(hit.score, float)

            assert store.search_fts("ghost", ["replication"], limit=10) == []
        finally:
            store.close()

    def test_exact_and_rare_term_match(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "doc-a",
                    [_chunk("doc-a", 0, "common intro text about databases")],
                    filename="A",
                )
            )
            store.add_document(
                _doc(
                    "doc-b",
                    [_chunk("doc-b", 0, "specialized discussion of xyzzy quux")],
                    filename="B",
                )
            )

            exact = store.search_fts("u1", ["databases"], limit=10)
            assert [h.doc_id for h in exact] == ["doc-a"]
            rare = store.search_fts("u1", ["xyzzy"], limit=10)
            assert [h.doc_id for h in rare] == ["doc-b"]
        finally:
            store.close()

    def test_unmatched_terms_no_results(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(_doc("doc-a", [_chunk("doc-a", 0, "databases")]))
            assert store.search_fts("u1", ["zzzznotaword"], limit=10) == []
            assert store.search_fts("u1", [], limit=10) == []
            assert store.search_fts("u1", ["   "], limit=10) == []
        finally:
            store.close()

    def test_multi_term_or_query(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc("doc-a", [_chunk("doc-a", 0, "kafka consumers")], filename="A")
            )
            store.add_document(
                _doc("doc-b", [_chunk("doc-b", 0, "redis caches")], filename="B")
            )

            hits = store.search_fts("u1", ["kafka", "redis"], limit=10)
            assert {h.doc_id for h in hits} == {"doc-a", "doc-b"}
        finally:
            store.close()

    def test_limit_respected_and_bm25_best_first(self, tmp_path: Path) -> None:
        strong = " ".join(["kafka streaming pipeline events"] * 6)
        medium = "a kafka note appears here once among filler words"
        weak = "another kafka mention inside a longer body of unrelated prose"
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc("doc-strong", [_chunk("doc-strong", 0, strong)], filename="S")
            )
            store.add_document(
                _doc("doc-medium", [_chunk("doc-medium", 0, medium)], filename="M")
            )
            store.add_document(
                _doc("doc-weak", [_chunk("doc-weak", 0, weak)], filename="W")
            )

            all_hits = store.search_fts("u1", ["kafka"], limit=10)
            assert len(all_hits) == 3
            limited = store.search_fts("u1", ["kafka"], limit=2)
            assert [h.chunk_id for h in limited] == [h.chunk_id for h in all_hits[:2]]
            assert all_hits[0].doc_id == "doc-strong"
            assert all_hits[0].score > all_hits[1].score
            assert store.search_fts("u1", ["kafka"], limit=0) == []
        finally:
            store.close()

    def test_equal_bm25_scores_tiebreak_by_chunk_id(self, tmp_path: Path) -> None:
        content = "identical duplicated prose about zebras"
        doc_ids = [f"doc-{i}" for i in range(8)]
        ingest_order = sorted(doc_ids, key=lambda d: chunk_id_for(d, 0), reverse=True)
        store = ChunkStore(tmp_path / "kb.db")
        try:
            for pos, doc_id in enumerate(ingest_order):
                store.add_document(
                    _doc(doc_id, [_chunk(doc_id, 0, content)], filename=doc_id)
                )

            hits = store.search_fts("u1", ["zebras"], limit=8)
            assert len(hits) == 8
            ids = [h.chunk_id for h in hits]
            assert ids == sorted(ids)
            expected_top3 = sorted(chunk_id_for(d, 0) for d in doc_ids)[:3]
            assert [
                h.chunk_id for h in store.search_fts("u1", ["zebras"], limit=3)
            ] == (expected_top3)
        finally:
            store.close()


class TestFtsAvailability:
    def test_fts_available_true_on_normal_build(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            assert store.fts_available is True
        finally:
            store.close()

    def test_missing_fts5_degrades_to_vector_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def raise_no_fts5(self: ChunkStore) -> None:
            raise sqlite3.OperationalError("no such module: fts5")

        monkeypatch.setattr(ChunkStore, "_create_fts_table", raise_no_fts5)

        with caplog.at_level(logging.WARNING, logger="vektor.retrieval.store"):
            store = ChunkStore(tmp_path / "kb.db")
        try:
            assert store.fts_available is False
            assert any("FTS5" in record.getMessage() for record in caplog.records)

            store.add_document(_doc("doc-1", [_chunk("doc-1", 0, "hello world")]))
            assert store.count() == 1
            assert store.search_fts("u1", ["hello"], limit=10) == []

            hits = store.search_vec("u1", [[1.0, 0.0, 0.0, 0.0]], limit=10)
            assert [h.chunk_id for h in hits] == [chunk_id_for("doc-1", 0)]
        finally:
            store.close()

    def test_non_fts5_operational_error_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_disk_error(self: ChunkStore) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(ChunkStore, "_create_fts_table", raise_disk_error)

        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            ChunkStore(tmp_path / "kb.db")

    def test_fts_backfill_after_vector_only_period(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Chunks ingested while FTS5 was down are searchable once it returns."""

        def raise_no_fts5(self: ChunkStore) -> None:
            raise sqlite3.OperationalError("no such module: fts5")

        monkeypatch.setattr(ChunkStore, "_create_fts_table", raise_no_fts5)
        store = ChunkStore(tmp_path / "kb.db")
        store.add_document(
            _doc(
                "doc-1",
                [
                    _chunk("doc-1", 0, "postgres replication notes"),
                    _chunk("doc-1", 1, "WAL archiving details"),
                ],
            )
        )
        assert store.fts_available is False
        assert store.search_fts("u1", ["replication"], limit=10) == []
        store.close()

        monkeypatch.undo()

        reopened = ChunkStore(tmp_path / "kb.db")
        try:
            assert reopened.fts_available is True
            hits = reopened.search_fts("u1", ["replication"], limit=10)
            assert [hit.chunk_id for hit in hits] == [chunk_id_for("doc-1", 0)]
            assert [
                hit.idx for hit in reopened.search_fts("u1", ["wal"], limit=10)
            ] == ([1])
        finally:
            reopened.close()

    def test_fts_backfill_is_not_repeated_on_reopen(self, tmp_path: Path) -> None:
        """An existing chunks_fts is never rebuilt — no duplicates, no drift fix."""
        db_path = tmp_path / "kb.db"
        store = ChunkStore(db_path)
        store.add_document(_doc("doc-1", [_chunk("doc-1", 0, "postgres replication")]))
        store.close()

        first = ChunkStore(db_path)
        try:
            assert len(first.search_fts("u1", ["replication"], limit=10)) == 1
        finally:
            first.close()

        second = ChunkStore(db_path)
        try:
            assert len(second.search_fts("u1", ["replication"], limit=10)) == 1
        finally:
            second.close()

    def test_fts_backfill_skipped_when_table_already_exists(
        self, tmp_path: Path
    ) -> None:
        """Backfill is tied to table creation, not a blanket reconciliation."""
        db_path = tmp_path / "kb.db"
        store = ChunkStore(db_path)
        store.add_document(_doc("doc-1", [_chunk("doc-1", 0, "searchable content")]))
        store._conn.execute("DELETE FROM chunks_fts")
        store._conn.commit()
        store.close()

        reopened = ChunkStore(db_path)
        try:
            assert reopened.fts_available is True
            assert reopened.search_fts("u1", ["searchable"], limit=10) == []
        finally:
            reopened.close()


class TestMetadataByIds:
    def test_returns_full_metadata_for_known_ids(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "doc-1",
                    [
                        _chunk("doc-1", 0, "intro to postgres replication"),
                        _chunk("doc-1", 1, "failover and WAL archiving"),
                    ],
                )
            )

            found = store.metadata_by_ids(
                [chunk_id_for("doc-1", 0), chunk_id_for("doc-1", 1)]
            )

            assert set(found) == {
                chunk_id_for("doc-1", 0),
                chunk_id_for("doc-1", 1),
            }
            first = found[chunk_id_for("doc-1", 0)]
            assert isinstance(first, ChunkHit)
            assert first.doc_id == "doc-1"
            assert first.title == "Doc title"
            assert first.idx == 0
            assert first.content == "intro to postgres replication"
            assert first.score == 0.0
        finally:
            store.close()

    def test_unknown_ids_omitted(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(_doc("doc-1", [_chunk("doc-1", 0, "content")]))

            found = store.metadata_by_ids(
                [chunk_id_for("doc-1", 0), chunk_id_for("ghost", 0)]
            )
            assert set(found) == {chunk_id_for("doc-1", 0)}
        finally:
            store.close()

    def test_empty_input_returns_empty_dict(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            assert store.metadata_by_ids([]) == {}
        finally:
            store.close()

    def test_large_id_batches_are_chunked(self, tmp_path: Path) -> None:
        total = 1900
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_document(
                _doc(
                    "bulk",
                    [_chunk("bulk", idx, f"bulk chunk {idx}") for idx in range(total)],
                )
            )

            ids = [chunk_id_for("bulk", idx) for idx in range(total)]
            found = store.metadata_by_ids(ids)

            assert len(found) == total
            assert found[chunk_id_for("bulk", 0)].content == "bulk chunk 0"
            assert found[chunk_id_for("bulk", total - 1)].idx == total - 1
        finally:
            store.close()


class TestMeta:
    def test_missing_key_returns_none(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            assert store.get_meta(META_EMBED_MODEL) is None
        finally:
            store.close()

    def test_set_get_roundtrip_and_overwrite(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.set_meta(META_EMBED_MODEL, "model-a")
            assert store.get_meta(META_EMBED_MODEL) == "model-a"
            store.set_meta(META_EMBED_MODEL, "model-b")
            assert store.get_meta(META_EMBED_MODEL) == "model-b"
            assert store.get_meta(META_EMBED_DIM) is None
        finally:
            store.close()

    def test_meta_survives_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "kb.db"
        store = ChunkStore(path)
        store.set_meta(META_EMBED_DIM, "7")
        store.close()

        reopened = ChunkStore(path)
        try:
            assert reopened.get_meta(META_EMBED_DIM) == "7"
        finally:
            reopened.close()

    def test_meta_works_in_vector_only_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def raise_no_fts5(self: ChunkStore) -> None:
            raise sqlite3.OperationalError("no such module: fts5")

        monkeypatch.setattr(ChunkStore, "_create_fts_table", raise_no_fts5)
        store = ChunkStore(tmp_path / "kb.db")
        try:
            assert store.fts_available is False
            store.set_meta(META_EMBED_MODEL, "m")
            assert store.get_meta(META_EMBED_MODEL) == "m"
        finally:
            store.close()


class TestLifecycle:
    def test_context_manager_closes(self, tmp_path: Path) -> None:
        with ChunkStore(tmp_path / "kb.db") as store:
            store.add_document(_doc("doc-1", [_chunk("doc-1", 0, "content")]))
            assert store.count() == 1

        with pytest.raises(sqlite3.ProgrammingError):
            store.count()

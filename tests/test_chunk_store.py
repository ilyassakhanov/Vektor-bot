"""Tests for ChunkStore — SQLite chunk storage with FTS5 sync and stable ids.

ChunkStore persists ChunkRecord rows in a `chunks` table and mirrors their
content into a standalone FTS5 virtual table keyed by chunk id, so keyword
search maps back to chunk metadata. Stable ids come from chunk_id_for
(sha256 over doc_id + ":" + idx), making re-ingest an upsert.

Covered acceptance criteria:
1. Insert keeps chunks and FTS tables in sync (count + search finds content).
2. Exact match and rare-term match found; unmatched terms yield no results;
   multi-term OR query works.
3. Re-ingesting the same doc_id + idx (same stable id) overwrites, never
   duplicates (count unchanged, new content searchable, old content gone).
4. search_fts respects limit and returns best-first (BM25) ordering.
5. fts_available reflects a successful init; a missing-FTS5 probe logs a
   warning, degrades to vector-only mode (add_chunks works, search_fts []).
6. BLOB round-trip: arbitrary bytes stored and returned via all_vectors.
7. chunk_id_for is deterministic; idx and doc_id changes alter the id.
8. meta table: set/get roundtrip, persistence across reopen, works in
   vector-only mode.
9. replace_chunks: replaces a whole document atomically — obsolete tail
   chunks (smaller new chunk count) are gone from chunks, FTS, and
   all_vectors; other documents are untouched; empty list is a no-op.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

import pytest

from retrieval.rrf import ChunkHit
from retrieval.store import ChunkRecord, ChunkStore, chunk_id_for


def _record(
    doc_id: str,
    idx: int,
    content: str,
    embedding: bytes = b"\x00\x01\x02",
    title: str = "Doc title",
) -> ChunkRecord:
    """Build a minimal ChunkRecord for tests."""
    return ChunkRecord(
        doc_id=doc_id,
        title=title,
        idx=idx,
        content=content,
        embedding=embedding,
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


class TestInsertAndSearch:
    def test_insert_syncs_chunks_and_fts(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            written = store.add_chunks(
                [
                    _record("doc-1", 0, "intro to postgres replication"),
                    _record("doc-1", 1, "failover and WAL archiving"),
                ]
            )

            assert written == 2
            assert store.count() == 2
            hits = store.search_fts(["replication"], limit=10)
            assert len(hits) == 1
            hit = hits[0]
            assert isinstance(hit, ChunkHit)
            assert hit.chunk_id == chunk_id_for("doc-1", 0)
            assert hit.doc_id == "doc-1"
            assert hit.title == "Doc title"
            assert hit.idx == 0
            assert hit.content == "intro to postgres replication"
            assert isinstance(hit.score, float)
        finally:
            store.close()

    def test_exact_and_rare_term_match(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks(
                [
                    _record("doc-a", 0, "common intro text about databases"),
                    _record("doc-b", 0, "specialized discussion of xyzzy quux"),
                ]
            )

            exact = store.search_fts(["databases"], limit=10)
            assert [h.doc_id for h in exact] == ["doc-a"]

            rare = store.search_fts(["xyzzy"], limit=10)
            assert [h.doc_id for h in rare] == ["doc-b"]
        finally:
            store.close()

    def test_unmatched_terms_no_results(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks([_record("doc-a", 0, "text about databases")])

            assert store.search_fts(["zzzznotaword"], limit=10) == []
        finally:
            store.close()

    def test_multi_term_or_query(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks(
                [
                    _record("doc-a", 0, "everything about kafka consumers"),
                    _record("doc-b", 0, "everything about redis caches"),
                ]
            )

            hits = store.search_fts(["kafka", "redis"], limit=10)
            assert {h.doc_id for h in hits} == {"doc-a", "doc-b"}
        finally:
            store.close()

    def test_empty_add_returns_zero(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            assert store.add_chunks([]) == 0
            assert store.count() == 0
        finally:
            store.close()


class TestUpsert:
    def test_reingest_overwrites_never_duplicates(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks([_record("doc-1", 0, "old content about postgres")])
            store.add_chunks(
                [_record("doc-1", 0, "new content about mongodb", embedding=b"\xff")]
            )

            assert store.count() == 1

            new_hits = store.search_fts(["mongodb"], limit=10)
            assert len(new_hits) == 1

            assert store.search_fts(["postgres"], limit=10) == []

            vectors = dict(store.all_vectors())
            assert vectors == {chunk_id_for("doc-1", 0): b"\xff"}
        finally:
            store.close()

    def test_other_chunks_untouched_by_upsert(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks(
                [
                    _record("doc-1", 0, "alpha content"),
                    _record("doc-1", 1, "beta content"),
                ]
            )
            store.add_chunks([_record("doc-1", 0, "alpha content revised")])

            assert store.count() == 2
            assert len(store.search_fts(["beta"], limit=10)) == 1
        finally:
            store.close()


class TestSearchOrderAndLimit:
    def test_limit_respected_and_bm25_best_first(self, tmp_path: Path) -> None:
        strong = " ".join(["kafka streaming pipeline events"] * 6)
        medium = "a kafka note appears here once among filler words"
        weak = "another kafka mention inside a longer body of unrelated prose"
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks(
                [
                    _record("doc-strong", 0, strong),
                    _record("doc-medium", 0, medium),
                    _record("doc-weak", 0, weak),
                ]
            )

            all_hits = store.search_fts(["kafka"], limit=10)
            assert len(all_hits) == 3

            limited = store.search_fts(["kafka"], limit=2)
            assert len(limited) == 2
            assert [h.chunk_id for h in limited] == [h.chunk_id for h in all_hits[:2]]

            assert all_hits[0].doc_id == "doc-strong"
            assert all_hits[0].score > all_hits[1].score
        finally:
            store.close()

    def test_equal_bm25_scores_tiebreak_by_chunk_id(self, tmp_path: Path) -> None:
        content = "identical duplicated prose about zebras"
        doc_ids = [f"doc-{i}" for i in range(8)]
        ingest_order = sorted(doc_ids, key=lambda d: chunk_id_for(d, 0), reverse=True)
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks([_record(d, 0, content) for d in ingest_order])

            hits = store.search_fts(["zebras"], limit=8)
            assert len(hits) == 8
            ids = [h.chunk_id for h in hits]
            assert ids == sorted(ids)

            expected_top3 = sorted(chunk_id_for(d, 0) for d in doc_ids)[:3]
            assert [h.chunk_id for h in store.search_fts(["zebras"], limit=3)] == (
                expected_top3
            )
        finally:
            store.close()

    def test_limit_zero_returns_empty(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks([_record("doc-a", 0, "kafka content")])

            assert store.search_fts(["kafka"], limit=0) == []
        finally:
            store.close()


class TestVectors:
    def test_blob_roundtrip_arbitrary_bytes(self, tmp_path: Path) -> None:
        blob = bytes(range(256)) + b"\x00\xff" * 17
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks([_record("doc-1", 0, "blob doc", embedding=blob)])

            assert store.all_vectors() == [(chunk_id_for("doc-1", 0), blob)]
        finally:
            store.close()

    def test_all_vectors_lists_every_chunk(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks(
                [
                    _record("doc-1", 0, "one", embedding=b"\x01"),
                    _record("doc-1", 1, "two", embedding=b"\x02"),
                    _record("doc-2", 0, "three", embedding=b"\x03"),
                ]
            )

            vectors = dict(store.all_vectors())
            assert vectors == {
                chunk_id_for("doc-1", 0): b"\x01",
                chunk_id_for("doc-1", 1): b"\x02",
                chunk_id_for("doc-2", 0): b"\x03",
            }
        finally:
            store.close()

    def test_all_vectors_empty_store(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            assert store.all_vectors() == []
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

            assert store.add_chunks([_record("doc-1", 0, "hello world")]) == 1
            assert store.count() == 1
            assert dict(store.all_vectors()) == {
                chunk_id_for("doc-1", 0): b"\x00\x01\x02"
            }
            assert store.search_fts(["hello"], limit=10) == []
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


class TestLifecycle:
    def test_context_manager_closes(self, tmp_path: Path) -> None:
        with ChunkStore(tmp_path / "kb.db") as store:
            store.add_chunks([_record("doc-1", 0, "content")])
            assert store.count() == 1

        with pytest.raises(sqlite3.ProgrammingError):
            store.count()


class TestMetadataByIds:
    def test_returns_full_metadata_for_known_ids(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks(
                [
                    _record("doc-1", 0, "intro to postgres replication"),
                    _record("doc-1", 1, "failover and WAL archiving", title="Other"),
                ]
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
            second = found[chunk_id_for("doc-1", 1)]
            assert second.idx == 1
            assert second.title == "Other"
        finally:
            store.close()

    def test_unknown_ids_omitted(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks([_record("doc-1", 0, "content")])

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
            store.add_chunks(
                [_record("bulk", idx, f"bulk chunk {idx}") for idx in range(total)]
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
            assert store.get_meta("embed_model") is None
        finally:
            store.close()

    def test_set_get_roundtrip_and_overwrite(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.set_meta("embed_model", "model-a")
            assert store.get_meta("embed_model") == "model-a"
            store.set_meta("embed_model", "model-b")
            assert store.get_meta("embed_model") == "model-b"
            assert store.get_meta("embed_dim") is None
        finally:
            store.close()

    def test_meta_survives_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "kb.db"
        store = ChunkStore(path)
        store.set_meta("embed_dim", "7")
        store.close()

        reopened = ChunkStore(path)
        try:
            assert reopened.get_meta("embed_dim") == "7"
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
            store.set_meta("embed_model", "m")
            assert store.get_meta("embed_model") == "m"
        finally:
            store.close()


class TestReplaceChunks:
    def test_replace_removes_obsolete_tail_chunks(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.replace_chunks(
                "doc-1",
                [
                    _record("doc-1", 0, "alpha content here"),
                    _record("doc-1", 1, "beta content here"),
                    _record("doc-1", 2, "gamma content here"),
                ],
            )
            assert store.count() == 3

            written = store.replace_chunks(
                "doc-1", [_record("doc-1", 0, "alpha content revised")]
            )

            assert written == 1
            assert store.count() == 1
            assert set(dict(store.all_vectors())) == {chunk_id_for("doc-1", 0)}
            assert store.search_fts(["beta"], limit=10) == []
            assert store.search_fts(["gamma"], limit=10) == []
            assert [h.idx for h in store.search_fts(["revised"], limit=10)] == [0]
        finally:
            store.close()

    def test_replace_keeps_other_documents(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks(
                [
                    _record("doc-1", 0, "first doc content"),
                    _record("doc-1", 1, "first doc tail"),
                    _record("doc-2", 0, "second doc content"),
                ]
            )

            store.replace_chunks("doc-1", [_record("doc-1", 0, "first doc replaced")])

            assert store.count() == 2
            hits = store.search_fts(["second"], limit=10)
            assert [h.doc_id for h in hits] == ["doc-2"]
            assert store.search_fts(["tail"], limit=10) == []
        finally:
            store.close()

    def test_replace_empty_list_is_noop(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.add_chunks(
                [
                    _record("doc-1", 0, "kept content"),
                    _record("doc-1", 1, "also kept"),
                ]
            )

            assert store.replace_chunks("doc-1", []) == 0
            assert store.count() == 2
            assert len(store.search_fts(["kept"], limit=10)) == 2
        finally:
            store.close()

    def test_replace_in_vector_only_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def raise_no_fts5(self: ChunkStore) -> None:
            raise sqlite3.OperationalError("no such module: fts5")

        monkeypatch.setattr(ChunkStore, "_create_fts_table", raise_no_fts5)
        store = ChunkStore(tmp_path / "kb.db")
        try:
            store.replace_chunks(
                "doc-1",
                [
                    _record("doc-1", 0, "one"),
                    _record("doc-1", 1, "two"),
                ],
            )
            store.replace_chunks("doc-1", [_record("doc-1", 0, "only")])

            assert store.count() == 1
            assert set(dict(store.all_vectors())) == {chunk_id_for("doc-1", 0)}
        finally:
            store.close()

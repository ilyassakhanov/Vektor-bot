"""Tests for KB user isolation — SQL-level owner scoping on every store path.

Two users share one store; every search API takes a mandatory user_id.
Covered: same-text ingest (colliding document id, no cross-owner leaks),
scoping of vector/FTS/metadata/list, delete ownership (non-owner → False).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from retrieval.store import (
    ChunkRecord,
    ChunkStore,
    DocumentRecord,
    DocumentRow,
    chunk_id_for,
    to_blob,
)

_DOC_TEXT = "alpha bravo charlie delta echo foxtrot"
_QUERY = [1.0, 0.0, 0.0, 0.0]


def _chunk(doc_id: str, idx: int, text: str, marker: float) -> ChunkRecord:
    return ChunkRecord(
        document_id=doc_id,
        chunk_index=idx,
        text=text,
        embedding=to_blob([marker, 0.0, 0.0, 0.0]),
    )


def _add(
    store: ChunkStore,
    user_id: str,
    doc_id: str,
    text: str,
    marker: float,
    filename: str = "notes.txt",
) -> None:
    store.add_document(
        DocumentRecord(
            id=doc_id,
            user_id=user_id,
            filename=filename,
            file_type="txt",
            created_at="2026-01-01T00:00:00+00:00",
            chunks=[_chunk(doc_id, 0, text, marker)],
        )
    )


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[ChunkStore]:
    s = ChunkStore(tmp_path / "kb.db")
    yield s
    s.close()


class TestSameTextIngest:
    def test_last_writer_owns_colliding_document(self, store: ChunkStore) -> None:
        doc_id = "collidingdoc"
        _add(store, "alice", doc_id, _DOC_TEXT, 1.0)
        assert len(store.search_vec("alice", [_QUERY], limit=5)) == 1

        _add(store, "bob", doc_id, _DOC_TEXT, 1.0)

        assert store.search_vec("alice", [_QUERY], limit=5) == []
        assert store.search_fts("alice", ["alpha"], limit=5) == []
        assert store.list_documents("alice") == []

        assert len(store.search_vec("bob", [_QUERY], limit=5)) == 1
        assert len(store.search_fts("bob", ["alpha"], limit=5)) == 1
        assert [d.chunk_count for d in store.list_documents("bob")] == [1]


class TestDifferentDocumentsScoping:
    def test_vec_search_returns_only_own_chunks(self, store: ChunkStore) -> None:
        _add(store, "alice", "doc-a", "alice secret project notes", 1.0)
        _add(store, "bob", "doc-b", "bob public garden notes", 0.5)

        alice_hits = store.search_vec("alice", [_QUERY], limit=10)
        assert [h.doc_id for h in alice_hits] == ["doc-a"]
        assert alice_hits[0].title == "notes.txt"
        assert "alice secret" in alice_hits[0].content

        bob_hits = store.search_vec("bob", [_QUERY], limit=10)
        assert [h.doc_id for h in bob_hits] == ["doc-b"]
        assert "bob public" in bob_hits[0].content

    def test_fts_search_returns_only_own_chunks(self, store: ChunkStore) -> None:
        _add(store, "alice", "doc-a", "kafka consumer lag explained", 1.0)
        _add(store, "bob", "doc-b", "kafka producer idempotence explained", 0.5)

        alice_hits = store.search_fts("alice", ["kafka", "explained"], limit=10)
        assert [h.doc_id for h in alice_hits] == ["doc-a"]
        assert alice_hits[0].chunk_id == chunk_id_for("doc-a", 0)

        bob_hits = store.search_fts("bob", ["kafka", "explained"], limit=10)
        assert [h.doc_id for h in bob_hits] == ["doc-b"]

    def test_metadata_stays_scoped_through_search(self, store: ChunkStore) -> None:
        _add(store, "alice", "doc-a", "shared keyword here", 1.0, filename="a.txt")
        _add(store, "bob", "doc-b", "shared keyword here", 0.5, filename="b.txt")

        for hit in store.search_fts("alice", ["shared"], limit=10):
            assert hit.doc_id == "doc-a"
            assert hit.title == "a.txt"
        for hit in store.search_vec("alice", [_QUERY], limit=10):
            assert hit.doc_id == "doc-a"
            assert hit.title == "a.txt"

    def test_search_without_matches_for_owner_is_empty(self, store: ChunkStore) -> None:
        _add(store, "alice", "doc-a", "quantum tunneling basics", 1.0)

        assert store.search_fts("bob", ["quantum"], limit=10) == []
        assert store.search_vec("bob", [_QUERY], limit=10) == []


class TestDeleteOwnership:
    def test_non_owner_cannot_delete(self, store: ChunkStore) -> None:
        _add(
            store,
            "alice",
            "doc-a",
            "alice private archive",
            1.0,
            filename="private.txt",
        )

        assert store.delete_document("bob", "private.txt") is False

        assert len(store.search_fts("alice", ["private"], limit=5)) == 1
        assert [d.filename for d in store.list_documents("alice")] == ["private.txt"]

    def test_owner_delete_leaves_other_users_intact(self, store: ChunkStore) -> None:
        _add(store, "alice", "doc-a", "alice zoo animals list", 1.0, filename="zoo.txt")
        _add(store, "bob", "doc-b", "bob zoo animals list", 0.5, filename="zoo.txt")

        assert store.delete_document("alice", "zoo.txt") is True

        assert store.search_fts("alice", ["zoo"], limit=5) == []
        assert store.search_vec("alice", [_QUERY], limit=5) == []
        assert store.list_documents("alice") == []

        assert len(store.search_fts("bob", ["zoo"], limit=5)) == 1
        assert len(store.search_vec("bob", [_QUERY], limit=5)) == 1
        assert [d.chunk_count for d in store.list_documents("bob")] == [1]

    def test_missing_filename_returns_false(self, store: ChunkStore) -> None:
        assert store.delete_document("alice", "never-uploaded.txt") is False


class TestListDocumentsScoping:
    def test_each_user_sees_only_their_documents(self, store: ChunkStore) -> None:
        _add(store, "alice", "doc-a", "alpha one two three", 1.0, filename="a.txt")
        _add(store, "alice", "doc-a2", "alpha four five", 0.9, filename="a2.txt")
        _add(store, "bob", "doc-b", "bravo one two three", 0.5, filename="b.txt")

        alice = store.list_documents("alice")
        assert sorted(d.filename for d in alice) == ["a.txt", "a2.txt"]
        assert all(isinstance(d, DocumentRow) for d in alice)
        assert {d.chunk_count for d in alice} == {1}

        bob = store.list_documents("bob")
        assert [d.filename for d in bob] == ["b.txt"]

        assert store.list_documents("carol") == []

"""Smoke tests for the sqlite-vec load helper (retrieval.vec).

Verifies extension load, vec0 DDL with an aux owner column, plain and
owner-filtered KNN on a tmp connection; skips when sqlite-vec is not
installed in the environment.
"""

from __future__ import annotations

import sqlite3
import struct

import pytest

from retrieval.vec import load

pytest.importorskip("sqlite_vec")


def _pack(vec: list[float]) -> bytes:
    """Serialize a vector to the little-endian float32 blob vec0 expects."""
    return struct.pack(f"{len(vec)}f", *vec)


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(tmp_path / "vec.db"))
    load(connection)
    return connection


def test_load_exposes_vec_version(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT vec_version()").fetchone()
    assert row is not None
    assert str(row[0]).startswith("v")


def test_knn_query_works(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE VIRTUAL TABLE t USING vec0(id TEXT PRIMARY KEY, owner TEXT, emb FLOAT[4])"
    )
    conn.executemany(
        "INSERT INTO t(id, owner, emb) VALUES (?, ?, ?)",
        [
            ("a1", "alice", _pack([1.0, 0.0, 0.0, 0.0])),
            ("b1", "bob", _pack([0.0, 1.0, 0.0, 0.0])),
        ],
    )
    rows = conn.execute(
        "SELECT id FROM t WHERE emb MATCH ? AND k = 1", (_pack([1.0, 0.0, 0.0, 0.0]),)
    ).fetchall()
    assert rows == [("a1",)]


def test_knn_aux_owner_filter(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE VIRTUAL TABLE t USING vec0(id TEXT PRIMARY KEY, owner TEXT, emb FLOAT[4])"
    )
    conn.executemany(
        "INSERT INTO t(id, owner, emb) VALUES (?, ?, ?)",
        [
            ("a1", "alice", _pack([1.0, 0.0, 0.0, 0.0])),
            ("b1", "bob", _pack([0.9, 0.1, 0.0, 0.0])),
            ("b2", "bob", _pack([0.0, 0.0, 1.0, 0.0])),
        ],
    )
    rows = conn.execute(
        "SELECT id FROM t WHERE owner = ? AND emb MATCH ? AND k = 2",
        ("bob", _pack([1.0, 0.0, 0.0, 0.0])),
    ).fetchall()
    assert {row[0] for row in rows} == {"b1", "b2"}

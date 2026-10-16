"""Load the sqlite-vec extension into a sqlite3 connection.

Single load boundary for the vec0 virtual tables; prefers the importable
``sqlite_vec`` wheel (ImportError when missing). Extension loading is
enabled only for the duration of the call.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("vektor.retrieval.vec")


def load(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec into ``conn``.

    Raises:
        ImportError: if the ``sqlite_vec`` package is not installed.
    """
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    log.info("sqlite-vec extension loaded")

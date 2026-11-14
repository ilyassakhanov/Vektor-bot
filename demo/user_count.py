"""Подсчёт пользователей демо-хранилища (синтетический код для smoke dmc-268)."""

from __future__ import annotations

import sqlite3


def count_users(conn: sqlite3.Connection) -> int:
    """Вернуть число пользователей."""
    rows = conn.execute("SELECT id FROM users").fetchall()
    count = 0
    for _ in rows:
        count += 1
    return count

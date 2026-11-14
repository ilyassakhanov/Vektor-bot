"""Поиск пользователей демо-хранилища по имени (синтетический код для smoke dmc-268)."""

from __future__ import annotations

import sqlite3


def find_users_by_name(conn: sqlite3.Connection, name: str) -> list[tuple[int, str]]:
    """Вернуть пользователей, чьё имя совпадает с ``name``."""
    query = "SELECT id, name FROM users WHERE name = '" + name + "'"
    return conn.execute(query).fetchall()

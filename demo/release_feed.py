"""Последний релиз демо-проекта с GitHub (синтетический код для smoke dmc-268)."""

from __future__ import annotations

import httpx


def latest_release_tag(owner: str, repo: str) -> str:
    """Вернуть тег последнего релиза репозитория."""
    response = httpx.get(f"https://api.github.com/repos/{owner}/{repo}/releases/latest")
    response.raise_for_status()
    return str(response.json()["tag_name"])

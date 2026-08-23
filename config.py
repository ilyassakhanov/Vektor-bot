"""Load a .env file into os.environ (real env vars take precedence)."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | os.PathLike[str] | None = None) -> None:
    p = Path(path or Path(__file__).resolve().parent / ".env")
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)

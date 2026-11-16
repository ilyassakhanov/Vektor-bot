"""Load a .env file into os.environ (real env vars take precedence).

Also holds ``positive_env``, the shared warn-and-fallback parser for
positive numeric env knobs (stdlib only, so any module can import it).
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

log = logging.getLogger("vektor.config")

_N = TypeVar("_N", int, float)


# Upper bound for timeout knobs read with ``positive_env``: larger values
# overflow downstream timeout APIs (``Future.result``, socket timeouts).
MAX_TIMEOUT_S = 3600.0

# Default for ``EXEC_TIMEOUT`` (exec tool, CVE server httpx calls, MCP round-trip).
DEFAULT_EXEC_TIMEOUT_S = 30.0


def positive_env(
    name: str,
    default: _N,
    parse: Callable[[str], _N],
    maximum: _N | None = None,
) -> _N:
    """Read a positive, finite number from env ``name`` using ``parse``.

    Unset returns ``default`` silently; a value that does not parse, is not
    finite (or too large to be represented as a float), or is ``<= 0`` logs a
    warning and returns ``default``. With ``maximum``, a valid value above it
    logs a warning and is clamped to ``maximum`` (same as ``BOT_WORKER_THREADS``).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = parse(raw)
        valid = math.isfinite(value) and value > 0
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        log.warning("Invalid %s %r, using default %s", name, raw, default)
        return default
    if maximum is not None and value > maximum:
        log.warning("%s %r is above the maximum, using %s", name, raw, maximum)
        return maximum
    return value


def exec_timeout_from_env() -> float:
    """Resolve ``EXEC_TIMEOUT`` — default 30; invalid/``<= 0``/NaN/inf warn and
    use the default; above :data:`MAX_TIMEOUT_S` warn and clamp."""
    return positive_env("EXEC_TIMEOUT", DEFAULT_EXEC_TIMEOUT_S, float, MAX_TIMEOUT_S)


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

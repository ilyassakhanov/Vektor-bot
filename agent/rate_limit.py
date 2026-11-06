"""Per-user rate limiting — one in-flight run plus a sliding message window.

Consulted at the Telegram handler boundary (``bot.handle_message``) after the
auth gate. A user may have at most one message being handled at a time
(``in_flight``) and at most ``max_per_window`` accepted messages per sliding
``window_s`` seconds (``window``). Only accepted acquisitions consume a window
slot, so a rejected message never extends a user's lockout.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("vektor.agent.rate_limit")

DEFAULT_MAX_PER_WINDOW = 10
DEFAULT_WINDOW_S = 60.0


@dataclass(frozen=True)
class RateDecision:
    """Outcome of :meth:`RateLimiter.try_acquire`.

    ``reason`` is ``None`` when ``ok``, else ``"in_flight"`` or ``"window"``.
    """

    ok: bool
    reason: str | None = None


class RateLimiter:
    """Thread-safe per-user limiter: one in-flight run + a sliding window."""

    def __init__(
        self,
        max_per_window: int = DEFAULT_MAX_PER_WINDOW,
        window_s: float = DEFAULT_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_per_window < 1:
            raise ValueError("max_per_window must be >= 1")
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        self._max = max_per_window
        self._window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._accepted: dict[str, deque[float]] = {}

    def try_acquire(self, user_id: str) -> RateDecision:
        """Admit ``user_id`` or say why not. An admitted user must :meth:`release`."""
        with self._lock:
            if user_id in self._in_flight:
                return RateDecision(ok=False, reason="in_flight")
            now = self._clock()
            stamps = self._accepted.setdefault(user_id, deque())
            cutoff = now - self._window_s
            while stamps and stamps[0] <= cutoff:
                stamps.popleft()
            if len(stamps) >= self._max:
                return RateDecision(ok=False, reason="window")
            stamps.append(now)
            self._in_flight.add(user_id)
            return RateDecision(ok=True)

    def release(self, user_id: str) -> None:
        """End ``user_id``'s in-flight run. Idempotent."""
        with self._lock:
            self._in_flight.discard(user_id)

"""Loki push handler — batches log records to a Loki gateway via httpx."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("vektor.loki_handler")

_DEFAULT_LABELS = {"job": "vektor", "app": "vektor-bot"}
_SELF_LOGGER_PREFIX = "vektor.loki_handler"
_DEFAULT_FLUSH_INTERVAL = 5.0
_DEFAULT_FLUSH_THRESHOLD = 100


class LokiPushHandler(logging.Handler):
    """Buffers formatted log records and pushes batches to Loki."""

    def __init__(
        self,
        url: str | None,
        labels: dict[str, str] | None = None,
        client: httpx.Client | None = None,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL,
        flush_threshold: int = _DEFAULT_FLUSH_THRESHOLD,
    ) -> None:
        super().__init__()
        self._url = url
        self._labels = labels or _DEFAULT_LABELS
        self._buffer: list[tuple[str, str]] = []
        self._client = client or httpx.Client(timeout=5.0)
        self._flush_interval = flush_interval
        self._flush_threshold = flush_threshold
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None
        if url is not None:
            self._thread = threading.Thread(
                target=self._flush_loop, name="vektor-loki-flush", daemon=True
            )
            self._thread.start()

    def _flush_loop(self) -> None:
        while not self._stop.wait(self._flush_interval):
            try:
                self.flush()
            except Exception:
                log.warning("Loki periodic flush failed", exc_info=True)

    def emit(self, record: logging.LogRecord) -> None:
        if self._url is None:
            return
        if record.name == _SELF_LOGGER_PREFIX or record.name.startswith(
            _SELF_LOGGER_PREFIX + "."
        ):
            return
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)
            return
        with self._lock:
            self._buffer.append((str(time.time_ns()), line))
            pending = len(self._buffer)
        if pending >= self._flush_threshold:
            self.flush()

    def pending(self) -> int:
        """Return the number of buffered records waiting to be pushed."""
        return len(self._buffer)

    def flush(self) -> None:
        if self._url is None:
            return
        with self._lock:
            if not self._buffer:
                return
            batch = self._buffer
            self._buffer = []
        payload: dict[str, Any] = {
            "streams": [
                {
                    "stream": self._labels,
                    "values": [[ts, line] for ts, line in batch],
                }
            ]
        }
        try:
            resp = self._client.post(self._url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Loki push failed, dropping %d records: %s", len(batch), exc)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._flush_interval + 5.0)
        try:
            self.flush()
        finally:
            self._client.close()
            super().close()

"""Logging configuration — JSON formatter, sensitive-data redaction, Loki wiring."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("vektor.logging_config")

_SENSITIVE_KEYWORDS = (
    "text",
    "message",
    "prompt",
    "content",
    "command",
    "arguments",
)
_REDACTED = "<redacted>"

_SPEC_RE = re.compile(
    r"%%|%(?:\((?P<mapkey>\w+)\))?(?P<spec>[-+#0 ]*(?:\d+)?(?:\.\d+)?[srdifxXeEgG])"
)
_KEYWORD_PREFIX_RE = re.compile(r"(\w+)=\s*$")
_PREFIX_WINDOW = 16


class JsonFormatter(logging.Formatter):
    """Formats a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class RedactionFilter(logging.Filter):
    """Redacts values bound to sensitive keyword arguments before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.msg, str):
            return True
        if isinstance(record.args, tuple) and record.args:
            record.args = self._redact_tuple(record.msg, record.args)
        elif isinstance(record.args, dict):
            record.args = self._redact_dict(record.args)
        return True

    def _redact_tuple(self, msg: str, args: tuple[Any, ...]) -> tuple[Any, ...]:
        redacted = list(args)
        position = 0
        for match in _SPEC_RE.finditer(msg):
            if match.group("spec") is None:
                continue
            if match.group("mapkey") is not None:
                continue
            if self._prefix_is_sensitive(msg, match.start()) and position < len(
                redacted
            ):
                redacted[position] = _REDACTED
            position += 1
        return tuple(redacted)

    def _redact_dict(self, args: dict[Any, Any]) -> dict[Any, Any]:
        return {
            key: (_REDACTED if key in _SENSITIVE_KEYWORDS else value)
            for key, value in args.items()
        }

    def _prefix_is_sensitive(self, msg: str, spec_start: int) -> bool:
        window = msg[max(0, spec_start - _PREFIX_WINDOW) : spec_start]
        match = _KEYWORD_PREFIX_RE.search(window)
        if match is None:
            return False
        return match.group(1).lower() in _SENSITIVE_KEYWORDS


def configure_logging(loki_url: str | None, level: str = "INFO") -> None:
    """Configure the root logger: JSON output, redaction, optional Loki push."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    redaction = RedactionFilter()
    if not any(isinstance(f, RedactionFilter) for f in root.filters):
        root.addFilter(redaction)
    for existing in list(root.handlers):
        root.removeHandler(existing)
        with contextlib.suppress(Exception):
            existing.close()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(redaction)
    root.addHandler(handler)
    if loki_url:
        from loki_handler import LokiPushHandler

        loki = LokiPushHandler(url=loki_url)
        loki.setFormatter(JsonFormatter())
        loki.addFilter(redaction)
        root.addHandler(loki)
    log.debug("logging configured level=%s loki=%s", level, bool(loki_url))

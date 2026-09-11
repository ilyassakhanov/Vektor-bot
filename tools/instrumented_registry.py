"""Instrumented tool registry — records tool call metrics."""

from __future__ import annotations

import logging
import time
from typing import Any

import metrics
from tools.registry import ToolRegistry

log = logging.getLogger("vektor.tools.instrumented")

_ERROR_PREFIX = "Error:"


class InstrumentedToolRegistry(ToolRegistry):
    """ToolRegistry subclass that records call count, duration, and status."""

    def execute(self, name: str, **arguments: Any) -> str:
        start = time.perf_counter()
        result = super().execute(name, **arguments)
        elapsed = time.perf_counter() - start
        status = "error" if result.startswith(_ERROR_PREFIX) else "success"
        metrics.tool_calls_total.labels(tool_name=name, status=status).inc()
        metrics.tool_duration_seconds.labels(tool_name=name).observe(elapsed)
        return result

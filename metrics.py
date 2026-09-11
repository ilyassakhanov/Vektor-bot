"""Prometheus metrics for the Vektor observability layer."""

from __future__ import annotations

import logging

from prometheus_client import Counter, Histogram, start_http_server

log = logging.getLogger("vektor.metrics")

llm_tokens_total = Counter(
    "vektor_llm_tokens_total",
    "LLM tokens consumed",
    ["model", "direction"],
)

llm_latency_seconds = Histogram(
    "vektor_llm_latency_seconds",
    "LLM call latency in seconds",
    ["model"],
)

llm_cost_total = Counter(
    "vektor_llm_cost_total",
    "Notional estimated LLM cost in USD (local Ollama costs $0; prices are notional for comparison)",
    ["model"],
)

agent_iterations = Histogram(
    "vektor_agent_iterations",
    "Agent loop iterations per run",
    buckets=(1, 2, 3, 4, 5, 6, 7, 8, 10, 16),
)

agent_max_iterations_reached_total = Counter(
    "vektor_agent_max_iterations_reached_total",
    "Agent runs that hit the max iteration limit",
)

tool_calls_total = Counter(
    "vektor_tool_calls_total",
    "Tool calls by name and status",
    ["tool_name", "status"],
)

tool_duration_seconds = Histogram(
    "vektor_tool_duration_seconds",
    "Tool call duration in seconds",
    ["tool_name"],
)


def start_metrics_server(port: int) -> None:
    """Start the Prometheus metrics HTTP server on the given port."""
    start_http_server(port)
    log.info("Metrics server started on port %d", port)

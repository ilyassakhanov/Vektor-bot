"""Prometheus metrics for the Vektor observability layer."""

from __future__ import annotations

import logging

from prometheus_client import Counter, Gauge, Histogram, start_http_server

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

mcp_server_up = Gauge(
    "vektor_mcp_server_up",
    "MCP server subprocess state (1=up, 0=down)",
)

mcp_roundtrip_seconds = Histogram(
    "vektor_mcp_roundtrip_seconds",
    "MCP stdio round-trip latency in seconds",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

mcp_restarts_total = Counter(
    "vektor_mcp_restarts_total",
    "MCP server subprocess restarts",
)

retrieval_expansion_total = Counter(
    "vektor_retrieval_expansion_total",
    "Query expansion outcomes (ok = produced terms, fallback = expander ran but produced nothing new)",
    ["status"],
)

retrieval_rerank_total = Counter(
    "vektor_retrieval_rerank_total",
    "Rerank stage outcomes (ok = rescored, fallback = kept RRF order)",
    ["status"],
)

retrieval_latency_seconds = Histogram(
    "vektor_retrieval_latency_seconds",
    "Hybrid retrieval per-stage latency in seconds",
    ["stage"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

retrieval_results = Histogram(
    "vektor_retrieval_results",
    "Retrieval hit counts per source (before fusion) and fused final length",
    ["source"],
    buckets=(0, 1, 2, 5, 10, 20, 50, 100),
)


def start_metrics_server(port: int) -> None:
    """Start the Prometheus metrics HTTP server on the given port."""
    start_http_server(port)
    log.info("Metrics server started on port %d", port)

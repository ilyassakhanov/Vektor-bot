"""Tests for the Prometheus metrics definitions in metrics.py."""

from __future__ import annotations

from prometheus_client import REGISTRY, Counter, Histogram, generate_latest

import metrics


def test_all_metrics_defined():
    assert hasattr(metrics, "llm_tokens_total")
    assert hasattr(metrics, "llm_latency_seconds")
    assert hasattr(metrics, "llm_cost_total")
    assert hasattr(metrics, "agent_iterations")
    assert hasattr(metrics, "agent_max_iterations_reached_total")
    assert hasattr(metrics, "tool_calls_total")
    assert hasattr(metrics, "tool_duration_seconds")


def test_llm_tokens_total_is_counter():
    assert isinstance(metrics.llm_tokens_total, Counter)


def test_llm_latency_seconds_is_histogram():
    assert isinstance(metrics.llm_latency_seconds, Histogram)


def test_llm_cost_total_is_counter():
    assert isinstance(metrics.llm_cost_total, Counter)


def test_agent_iterations_is_histogram():
    assert isinstance(metrics.agent_iterations, Histogram)


def test_agent_max_iterations_reached_is_counter():
    assert isinstance(metrics.agent_max_iterations_reached_total, Counter)


def test_tool_calls_total_is_counter():
    assert isinstance(metrics.tool_calls_total, Counter)


def test_tool_duration_seconds_is_histogram():
    assert isinstance(metrics.tool_duration_seconds, Histogram)


def test_llm_tokens_total_labels():
    metrics.llm_tokens_total.labels(model="uniq-labels", direction="input").inc(1)
    value = REGISTRY.get_sample_value(
        "vektor_llm_tokens_total",
        {"model": "uniq-labels", "direction": "input"},
    )
    assert value == 1.0


def test_llm_cost_total_labels():
    metrics.llm_cost_total.labels(model="uniq-cost").inc(2.5)
    value = REGISTRY.get_sample_value("vektor_llm_cost_total", {"model": "uniq-cost"})
    assert value == 2.5


def test_llm_cost_total_help_mentions_notional_usd():
    output = generate_latest(REGISTRY).decode()
    help_line = next(
        line
        for line in output.splitlines()
        if line.startswith("# HELP vektor_llm_cost_total ")
    )
    assert "notional" in help_line.lower()
    assert "USD" in help_line


def test_tool_calls_labels():
    metrics.tool_calls_total.labels(tool_name="uniq-tool", status="success").inc(1)
    value = REGISTRY.get_sample_value(
        "vektor_tool_calls_total",
        {"tool_name": "uniq-tool", "status": "success"},
    )
    assert value == 1.0


def test_start_metrics_server_callable():
    assert callable(metrics.start_metrics_server)


def test_metric_names_have_vektor_prefix():
    metrics.llm_tokens_total.labels(model="uniq-names", direction="input").inc(1)
    metrics.llm_latency_seconds.labels(model="uniq-names").observe(0.1)
    metrics.llm_cost_total.labels(model="uniq-names").inc(1)
    metrics.tool_calls_total.labels(tool_name="uniq-names", status="success").inc(1)
    metrics.tool_duration_seconds.labels(tool_name="uniq-names").observe(0.1)
    assert (
        REGISTRY.get_sample_value(
            "vektor_llm_tokens_total", {"model": "uniq-names", "direction": "input"}
        )
        == 1.0
    )
    assert (
        REGISTRY.get_sample_value(
            "vektor_llm_latency_seconds_count", {"model": "uniq-names"}
        )
        == 1.0
    )
    assert (
        REGISTRY.get_sample_value("vektor_llm_cost_total", {"model": "uniq-names"})
        == 1.0
    )
    assert REGISTRY.get_sample_value("vektor_agent_iterations_count") is not None
    assert (
        REGISTRY.get_sample_value("vektor_agent_max_iterations_reached_total")
        is not None
    )
    assert (
        REGISTRY.get_sample_value(
            "vektor_tool_calls_total", {"tool_name": "uniq-names", "status": "success"}
        )
        == 1.0
    )
    assert (
        REGISTRY.get_sample_value(
            "vektor_tool_duration_seconds_count", {"tool_name": "uniq-names"}
        )
        == 1.0
    )

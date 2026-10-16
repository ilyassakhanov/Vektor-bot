"""Tests for the Prometheus metrics definitions in metrics.py."""

from __future__ import annotations

import time

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, generate_latest

import metrics
from retrieval.expansion import QueryExpander
from tests.fakes import FakeLLM
from tests.test_hybrid import FakeEmbedder, FakeFts, FakeVector, _hit, _retriever

_EXPANSION_TOTAL = "vektor_retrieval_expansion_total"
_LATENCY_COUNT = "vektor_retrieval_latency_seconds_count"
_LATENCY_SUM = "vektor_retrieval_latency_seconds_sum"
_RESULTS_COUNT = "vektor_retrieval_results_count"
_RESULTS_SUM = "vektor_retrieval_results_sum"


def _value(name: str, labels: dict[str, str] | None = None) -> float:
    """Current value of one sample (0.0 when the series does not exist yet)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_all_metrics_defined():
    assert hasattr(metrics, "llm_tokens_total")
    assert hasattr(metrics, "llm_latency_seconds")
    assert hasattr(metrics, "llm_cost_total")
    assert hasattr(metrics, "agent_iterations")
    assert hasattr(metrics, "agent_max_iterations_reached_total")
    assert hasattr(metrics, "tool_calls_total")
    assert hasattr(metrics, "tool_duration_seconds")
    assert hasattr(metrics, "mcp_server_up")
    assert hasattr(metrics, "mcp_roundtrip_seconds")
    assert hasattr(metrics, "mcp_restarts_total")


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


def test_mcp_server_up_is_gauge():
    assert isinstance(metrics.mcp_server_up, Gauge)
    metrics.mcp_server_up.set(0)
    metrics.mcp_server_up.set(1)


def test_mcp_roundtrip_seconds_is_histogram():
    assert isinstance(metrics.mcp_roundtrip_seconds, Histogram)
    metrics.mcp_roundtrip_seconds.observe(0.1)


def test_mcp_restarts_total_is_counter():
    assert isinstance(metrics.mcp_restarts_total, Counter)
    metrics.mcp_restarts_total.inc()


def test_mcp_metrics_have_vektor_prefix():
    metrics.mcp_server_up.set(1)
    metrics.mcp_roundtrip_seconds.observe(0.1)
    metrics.mcp_restarts_total.inc(1)
    assert REGISTRY.get_sample_value("vektor_mcp_server_up") == 1.0
    assert REGISTRY.get_sample_value("vektor_mcp_roundtrip_seconds_count") is not None
    assert REGISTRY.get_sample_value("vektor_mcp_restarts_total") is not None


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


# --- vektor_retrieval_* (HybridRetriever instrumentation) -----------------------


def test_retrieval_metrics_defined():
    assert hasattr(metrics, "retrieval_expansion_total")
    assert hasattr(metrics, "retrieval_latency_seconds")
    assert hasattr(metrics, "retrieval_results")
    assert isinstance(metrics.retrieval_expansion_total, Counter)
    assert isinstance(metrics.retrieval_latency_seconds, Histogram)
    assert isinstance(metrics.retrieval_results, Histogram)


def test_expansion_ok_recorded_with_expansion_latency():
    llm = FakeLLM(reply='{"keywords": ["kw"], "queries": ["alt q"]}')
    ok_before = _value(_EXPANSION_TOTAL, {"status": "ok"})
    fallback_before = _value(_EXPANSION_TOTAL, {"status": "fallback"})
    latency_before = _value(_LATENCY_COUNT, {"stage": "expansion"})

    result = _retriever(
        FakeEmbedder(),
        FakeVector(hits=[_hit("a")]),
        FakeFts(hits=[_hit("a")]),
        expander=QueryExpander(llm),
    ).search("metrics query", "user-1")

    assert result.used_expansion is True
    assert _value(_EXPANSION_TOTAL, {"status": "ok"}) == ok_before + 1
    assert _value(_EXPANSION_TOTAL, {"status": "fallback"}) == fallback_before
    assert _value(_LATENCY_COUNT, {"stage": "expansion"}) == latency_before + 1


def test_expansion_fallback_recorded_with_expansion_latency():
    llm = FakeLLM(reply='{"keywords": [], "queries": []}')
    ok_before = _value(_EXPANSION_TOTAL, {"status": "ok"})
    fallback_before = _value(_EXPANSION_TOTAL, {"status": "fallback"})
    latency_before = _value(_LATENCY_COUNT, {"stage": "expansion"})

    result = _retriever(
        FakeEmbedder(),
        FakeVector(hits=[_hit("a")]),
        FakeFts(hits=[_hit("a")]),
        expander=QueryExpander(llm),
    ).search("metrics query", "user-1")

    assert result.used_expansion is False
    assert _value(_EXPANSION_TOTAL, {"status": "ok"}) == ok_before
    assert _value(_EXPANSION_TOTAL, {"status": "fallback"}) == fallback_before + 1
    assert _value(_LATENCY_COUNT, {"stage": "expansion"}) == latency_before + 1


def test_no_expander_records_no_expansion_metrics():
    ok_before = _value(_EXPANSION_TOTAL, {"status": "ok"})
    fallback_before = _value(_EXPANSION_TOTAL, {"status": "fallback"})
    latency_before = _value(_LATENCY_COUNT, {"stage": "expansion"})

    _retriever(
        FakeEmbedder(), FakeVector(hits=[_hit("a")]), FakeFts(hits=[_hit("a")])
    ).search("metrics query", "user-1")

    assert _value(_EXPANSION_TOTAL, {"status": "ok"}) == ok_before
    assert _value(_EXPANSION_TOTAL, {"status": "fallback"}) == fallback_before
    assert _value(_LATENCY_COUNT, {"stage": "expansion"}) == latency_before


def test_source_and_total_stage_latencies_recorded():
    vector_before = _value(_LATENCY_COUNT, {"stage": "vector"})
    fts_before = _value(_LATENCY_COUNT, {"stage": "fts"})
    total_before = _value(_LATENCY_COUNT, {"stage": "total"})

    _retriever(
        FakeEmbedder(), FakeVector(hits=[_hit("a")]), FakeFts(hits=[_hit("a")])
    ).search("metrics query", "user-1")

    assert _value(_LATENCY_COUNT, {"stage": "vector"}) == vector_before + 1
    assert _value(_LATENCY_COUNT, {"stage": "fts"}) == fts_before + 1
    assert _value(_LATENCY_COUNT, {"stage": "total"}) == total_before + 1


def test_stage_latency_measures_search_execution_not_queue_wait():
    """Vector latency covers its own search body (incl. its 50ms sleep) while
    wall-clock proves both sources still ran concurrently — timing wraps the
    submitted callables, not future.result()."""
    vector = FakeVector(hits=[_hit("a")], delay=0.05)
    fts = FakeFts(hits=[_hit("b")], delay=0.05)
    vector_sum_before = _value(_LATENCY_SUM, {"stage": "vector"})

    start = time.perf_counter()
    _retriever(FakeEmbedder(), vector, fts).search("metrics query", "user-1")
    elapsed = time.perf_counter() - start

    assert _value(_LATENCY_SUM, {"stage": "vector"}) >= vector_sum_before + 0.05
    assert elapsed < 0.25


def test_failed_source_records_stage_latency_but_no_results_metric():
    vector_latency_before = _value(_LATENCY_COUNT, {"stage": "vector"})
    vector_results_before = _value(_RESULTS_COUNT, {"source": "vector"})
    fts_results_before = _value(_RESULTS_COUNT, {"source": "fts"})
    fts_sum_before = _value(_RESULTS_SUM, {"source": "fts"})

    result = _retriever(
        FakeEmbedder(),
        FakeVector(error=RuntimeError("index gone")),
        FakeFts(hits=[_hit("b"), _hit("a")]),
    ).search("metrics query", "user-1")

    assert result.sources_used == ("fts",)
    assert _value(_LATENCY_COUNT, {"stage": "vector"}) == vector_latency_before + 1
    assert _value(_RESULTS_COUNT, {"source": "vector"}) == vector_results_before
    assert _value(_RESULTS_COUNT, {"source": "fts"}) == fts_results_before + 1
    assert _value(_RESULTS_SUM, {"source": "fts"}) == fts_sum_before + 2


def test_results_counts_include_zero_hits_and_fused_final():
    vector = FakeVector(hits=[])
    fts = FakeFts(hits=[_hit("b"), _hit("a")])
    vector_count_before = _value(_RESULTS_COUNT, {"source": "vector"})
    vector_sum_before = _value(_RESULTS_SUM, {"source": "vector"})
    final_count_before = _value(_RESULTS_COUNT, {"source": "final"})
    final_sum_before = _value(_RESULTS_SUM, {"source": "final"})

    result = _retriever(FakeEmbedder(), vector, fts).search("metrics query", "user-1")

    assert _value(_RESULTS_COUNT, {"source": "vector"}) == vector_count_before + 1
    assert _value(_RESULTS_SUM, {"source": "vector"}) == vector_sum_before
    assert _value(_RESULTS_COUNT, {"source": "final"}) == final_count_before + 1
    assert _value(_RESULTS_SUM, {"source": "final"}) == final_sum_before + len(
        result.hits
    )


def test_vector_only_mode_records_no_fts_metrics():
    fts_latency_before = _value(_LATENCY_COUNT, {"stage": "fts"})
    fts_results_before = _value(_RESULTS_COUNT, {"source": "fts"})
    vector_results_before = _value(_RESULTS_COUNT, {"source": "vector"})
    total_before = _value(_LATENCY_COUNT, {"stage": "total"})

    result = _retriever(FakeEmbedder(), FakeVector(hits=[_hit("a")]), fts=None).search(
        "metrics query", "user-1"
    )

    assert result.sources_used == ("vector",)
    assert _value(_LATENCY_COUNT, {"stage": "fts"}) == fts_latency_before
    assert _value(_RESULTS_COUNT, {"source": "fts"}) == fts_results_before
    assert _value(_RESULTS_COUNT, {"source": "vector"}) == vector_results_before + 1
    assert _value(_LATENCY_COUNT, {"stage": "total"}) == total_before + 1


def test_no_query_or_expansion_text_in_metric_output():
    llm = FakeLLM(reply='{"keywords": ["secretword"], "queries": ["secretquery"]}')

    _retriever(
        FakeEmbedder(),
        FakeVector(hits=[_hit("a")]),
        FakeFts(hits=[_hit("a")]),
        expander=QueryExpander(llm),
    ).search("topsecret-query", "user-1")

    output = generate_latest(REGISTRY).decode()
    assert "topsecret-query" not in output
    assert "secretword" not in output
    assert "secretquery" not in output


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

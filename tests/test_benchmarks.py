"""Tests for the benchmark suite — prompts manifest, runner, results writer.

No network access and no real Ollama: the runner is exercised through
FakeLLM/ScriptedLLM-based agents, and the skip path uses 127.0.0.1:1 which
refuses connections instantly. The report and comparison-gate CLIs are
exercised on synthetic result rows via ``main()``/pure functions only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.agent import Agent
from llm.base import ChatResponse, ToolCall
from tests.fakes import FakeLLM, ScriptedLLM
from tools.base import Tool
from tools.registry import ToolRegistry

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_PATH = _PROJECT_ROOT / "benchmarks" / "prompts.json"


class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo the message argument."

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }

    def execute(self, **kwargs: object) -> str:
        return str(kwargs.get("message", ""))


def _make_registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


# --- Prompts manifest --------------------------------------------------------


def test_prompts_json_valid():
    data = json.loads(_PROMPTS_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) >= 6
    ids: set[str] = set()
    categories: set[str] = set()
    for entry in data:
        assert isinstance(entry, dict)
        assert isinstance(entry.get("id"), str)
        assert isinstance(entry.get("prompt"), str)
        assert isinstance(entry.get("category"), str)
        assert entry["prompt"]
        ids.add(entry["id"])
        categories.add(entry["category"])
    assert len(ids) == len(data)
    assert "chat" in categories
    assert "cve_tool" in categories


# --- Module import / entrypoint ----------------------------------------------


def test_benchmarks_run_imports():
    import benchmarks.run  # noqa: F401


def test_benchmarks_run_has_main():
    import benchmarks.run

    assert callable(benchmarks.run.main)


# --- run_prompt --------------------------------------------------------------


def test_run_prompt_records_result_structure():
    from benchmarks.run import CountingLLM, CountingRegistry, run_prompt

    counting = CountingLLM(FakeLLM(reply="benchmark reply"))
    registry = CountingRegistry()
    agent = Agent(counting, registry)
    entry = {"id": "chat-x", "category": "chat", "prompt": "Say something."}
    result = run_prompt(agent, counting, registry, entry)
    assert result["id"] == "chat-x"
    assert result["category"] == "chat"
    assert result["prompt"] == "Say something."
    assert isinstance(result["reply_len"], int)
    assert result["reply_len"] > 0
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert result["cached_tokens"] == 0
    assert isinstance(result["llm_calls"], int)
    assert result["llm_calls"] >= 1
    assert result["tool_calls"] == 0
    assert isinstance(result["latency_s"], float)
    assert result["latency_s"] >= 0


def test_run_prompt_counts_tool_calls():
    from benchmarks.run import CountingLLM, CountingRegistry, run_prompt

    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "hi"})
                ],
            ),
            ChatResponse(content="The echo said: hi"),
        ]
    )
    counting = CountingLLM(llm)
    registry = CountingRegistry()
    registry.register(EchoTool())
    agent = Agent(counting, registry)
    entry = {"id": "tool-x", "category": "chat", "prompt": "echo hi"}
    result = run_prompt(agent, counting, registry, entry)
    assert result["tool_calls"] == 1
    assert result["llm_calls"] == 2
    assert result["reply_len"] > 0


def test_run_prompt_accumulates_token_usage():
    from benchmarks.run import CountingLLM, CountingRegistry, run_prompt
    from llm.base import TokenUsage

    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "hi"})
                ],
            ),
            ChatResponse(
                content="done",
                usage=TokenUsage(input_tokens=30, output_tokens=7, cached_tokens=4),
            ),
        ]
    )
    counting = CountingLLM(llm)
    registry = CountingRegistry()
    registry.register(EchoTool())
    agent = Agent(counting, registry)
    entry = {"id": "tok-x", "category": "chat", "prompt": "echo hi"}
    result = run_prompt(agent, counting, registry, entry)
    assert result["input_tokens"] == 40
    assert result["output_tokens"] == 12
    assert result["cached_tokens"] == 4


# --- Per-call capture + ok/cost/turns + tool-result chars -------------------


def test_counting_llm_per_call_ordered():
    from benchmarks.run import CountingLLM
    from llm.base import TokenUsage

    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                usage=TokenUsage(input_tokens=11, output_tokens=3, model="m1"),
            ),
            ChatResponse(
                content="done",
                usage=TokenUsage(
                    input_tokens=22, output_tokens=4, cached_tokens=2, model="m2"
                ),
            ),
        ]
    )
    counting = CountingLLM(llm)
    counting.chat([], [])
    counting.chat([], [])
    assert counting.calls == 2
    assert [u.input_tokens for u in counting.per_call] == [11, 22]
    assert counting.per_call[0].output_tokens == 3
    assert counting.per_call[0].model == "m1"
    assert counting.per_call[1].cached_tokens == 2
    counting.reset()
    assert counting.per_call == []


def test_run_prompt_records_per_call_in_row():
    from benchmarks.run import CountingLLM, CountingRegistry, run_prompt
    from llm.base import TokenUsage

    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                usage=TokenUsage(
                    input_tokens=15, output_tokens=5, latency_ns=1000, model="ma"
                ),
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "hi"})
                ],
            ),
            ChatResponse(
                content="done",
                usage=TokenUsage(
                    input_tokens=25,
                    output_tokens=7,
                    cached_tokens=6,
                    latency_ns=2000,
                    model="mb",
                ),
            ),
        ]
    )
    counting = CountingLLM(llm)
    registry = CountingRegistry()
    registry.register(EchoTool())
    agent = Agent(counting, registry)
    entry = {"id": "pc-x", "category": "chat", "prompt": "echo hi"}
    result = run_prompt(agent, counting, registry, entry)
    assert result["per_call"] == [
        {
            "input_tokens": 15,
            "output_tokens": 5,
            "cached_tokens": 0,
            "latency_ns": 1000,
            "model": "ma",
        },
        {
            "input_tokens": 25,
            "output_tokens": 7,
            "cached_tokens": 6,
            "latency_ns": 2000,
            "model": "mb",
        },
    ]
    assert isinstance(json.dumps(result["per_call"]), str)


def test_run_prompt_ok_true_on_final_answer():
    from benchmarks.run import CountingLLM, CountingRegistry, run_prompt

    counting = CountingLLM(FakeLLM(reply="benchmark reply"))
    registry = CountingRegistry()
    agent = Agent(counting, registry)
    entry = {"id": "ok-x", "category": "chat", "prompt": "Say something."}
    result = run_prompt(agent, counting, registry, entry)
    assert result["ok"] is True
    assert result["failure"] == ""


def test_run_prompt_ok_false_on_max_iterations_fallback():
    from benchmarks.run import CountingLLM, CountingRegistry, run_prompt

    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id=f"tc{i}", name="echo", arguments={"message": "x"})
                ],
            )
            for i in range(100)
        ]
    )
    counting = CountingLLM(llm)
    registry = CountingRegistry()
    registry.register(EchoTool())
    agent = Agent(counting, registry, max_iterations=3)
    entry = {"id": "loop-x", "category": "chat", "prompt": "loop"}
    result = run_prompt(agent, counting, registry, entry, max_iterations=3)
    assert result["ok"] is False
    assert result["failure"] == "max_iterations"
    assert result["turns"] == result["llm_calls"]
    assert result["turns"] == 3


def test_run_prompt_ok_false_on_empty_reply():
    from benchmarks.run import CountingLLM, CountingRegistry, run_prompt

    llm = ScriptedLLM([ChatResponse(content="")])
    counting = CountingLLM(llm)
    registry = CountingRegistry()
    agent = Agent(counting, registry)
    entry = {"id": "empty-x", "category": "chat", "prompt": "Say nothing."}
    result = run_prompt(agent, counting, registry, entry)
    assert result["ok"] is False
    assert result["failure"] == "empty_reply"


def test_run_prompt_cost_summed_over_per_call(monkeypatch):
    import pytest

    from benchmarks.run import CountingLLM, CountingRegistry, run_prompt
    from llm.base import TokenUsage
    from llm.cost import estimate_cost

    monkeypatch.setenv("LLM_PRICE_IN_PER_1M", "1.0")
    monkeypatch.setenv("LLM_PRICE_OUT_PER_1M", "2.0")
    first = TokenUsage(input_tokens=1_000_000, output_tokens=0, model="llama3.2")
    second = TokenUsage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cached_tokens=500_000,
        model="llama3.2",
    )
    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                usage=first,
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "hi"})
                ],
            ),
            ChatResponse(content="done", usage=second),
        ]
    )
    counting = CountingLLM(llm)
    registry = CountingRegistry()
    registry.register(EchoTool())
    agent = Agent(counting, registry)
    entry = {"id": "cost-x", "category": "chat", "prompt": "echo hi"}
    result = run_prompt(agent, counting, registry, entry)
    expected = estimate_cost(first) + estimate_cost(second)
    assert expected == pytest.approx(1.0 + 0.5 + 2.0)
    assert result["cost"] == pytest.approx(expected)


def test_run_prompt_records_tool_result_chars():
    from benchmarks.run import CountingLLM, CountingRegistry, run_prompt

    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "hello"})
                ],
            ),
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc2", name="echo", arguments={"message": "goodbye"})
                ],
            ),
            ChatResponse(content="done"),
        ]
    )
    counting = CountingLLM(llm)
    registry = CountingRegistry()
    registry.register(EchoTool())
    agent = Agent(counting, registry)
    entry = {"id": "chars-x", "category": "chat", "prompt": "echo twice"}
    result = run_prompt(agent, counting, registry, entry)
    assert result["tool_result_chars"] == {"echo": len("hello") + len("goodbye")}


def test_result_row_json_roundtrip():
    from benchmarks.run import CountingLLM, CountingRegistry, run_prompt
    from llm.base import TokenUsage

    llm = ScriptedLLM(
        [
            ChatResponse(
                content="",
                usage=TokenUsage(
                    input_tokens=9, output_tokens=2, cached_tokens=1, model="rt"
                ),
                tool_calls=[
                    ToolCall(id="tc1", name="echo", arguments={"message": "hi"})
                ],
            ),
            ChatResponse(content="done"),
        ]
    )
    counting = CountingLLM(llm)
    registry = CountingRegistry()
    registry.register(EchoTool())
    agent = Agent(counting, registry)
    entry = {"id": "rt-x", "category": "chat", "prompt": "echo hi"}
    result = run_prompt(agent, counting, registry, entry)
    roundtrip = json.loads(json.dumps(result))
    for field in ("ok", "failure", "cost", "turns", "per_call", "tool_result_chars"):
        assert field in result
        assert roundtrip[field] == result[field]


# --- Results writer ----------------------------------------------------------


def test_results_writer_outputs_json(tmp_path):
    from benchmarks.run import write_results

    results = [
        {"id": "a", "category": "chat", "latency_s": 1.5},
        {"id": "b", "category": "cve_tool", "latency_s": 2.25},
    ]
    path = tmp_path / "results.json"
    write_results(results, path)
    assert path.exists()
    with path.open(encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded == results


# --- Skip path when Ollama is unreachable ------------------------------------


def test_main_skips_when_ollama_unreachable(tmp_path, capsys):
    from benchmarks.run import main

    out = tmp_path / "results.json"
    code = main(["--output", str(out), "--base-url", "http://127.0.0.1:1"])
    assert code == 0
    assert not out.exists()
    captured = capsys.readouterr()
    assert "Skipping benchmarks" in captured.out


def test_ollama_reachable_false_for_closed_port():
    from benchmarks.run import ollama_reachable

    assert ollama_reachable("http://127.0.0.1:1") is False


# --- T4a/T4b: audit report math + CLI -----------------------------------------


def _result_row(
    row_id: str = "task-x",
    category: str = "chat",
    per_call_inputs: list[int] | None = None,
    cached_tokens: int = 0,
    tool_result_chars: dict[str, int] | None = None,
    ok: bool = True,
    cost: float | None = None,
    tool_calls: int = 0,
) -> dict[str, Any]:
    """Build a synthetic result row matching the run_prompt field schema."""
    if per_call_inputs is None:
        per_call_inputs = [100]
    per_call = [
        {
            "input_tokens": usage_input,
            "output_tokens": 3,
            "cached_tokens": 0,
            "latency_ns": 1000,
            "model": "test-model",
        }
        for usage_input in per_call_inputs
    ]
    return {
        "id": row_id,
        "category": category,
        "prompt": "synthetic prompt",
        "reply_len": 12,
        "input_tokens": sum(per_call_inputs),
        "output_tokens": 3 * len(per_call_inputs),
        "cached_tokens": cached_tokens,
        "llm_calls": len(per_call_inputs),
        "tool_calls": tool_calls,
        "latency_s": 0.5,
        "ok": ok,
        "failure": "" if ok else "max_iterations",
        "cost": 0.001 * len(per_call_inputs) if cost is None else cost,
        "turns": len(per_call_inputs),
        "per_call": per_call,
        "tool_result_chars": dict(tool_result_chars or {}),
    }


def test_cache_hit_rate():
    from benchmarks.report import cache_hit_rate

    rows = [
        _result_row(per_call_inputs=[200, 100], cached_tokens=60),
        _result_row(per_call_inputs=[100], cached_tokens=40),
    ]
    assert cache_hit_rate(rows) == pytest.approx(100 / 400)


def test_cache_hit_rate_zero_when_no_input():
    from benchmarks.report import cache_hit_rate

    rows = [_result_row(per_call_inputs=[0], cached_tokens=0)]
    assert cache_hit_rate(rows) == 0.0


def test_repeated_context_share_multi_turn():
    from benchmarks.report import repeated_context

    result = repeated_context([_result_row(per_call_inputs=[100, 120, 150])])
    assert result.repeated == 220
    assert result.new == 50
    assert result.total == 370


def test_repeated_context_share_single_turn_is_zero():
    from benchmarks.report import repeated_context

    result = repeated_context([_result_row(per_call_inputs=[100])])
    assert result.repeated == 0
    assert result.new == 100
    assert result.new == result.total


def test_tool_token_shares_sorted():
    from benchmarks.report import tool_token_shares

    rows = [
        _result_row(tool_result_chars={"exec": 200}),
        _result_row(tool_result_chars={"exec": 200, "get_latest_cve": 100}),
    ]
    shares = tool_token_shares(rows)
    assert [share.name for share in shares] == ["exec", "get_latest_cve"]
    assert shares[0].tokens == 100
    assert shares[0].share == pytest.approx(0.8)
    assert shares[1].tokens == 25
    assert shares[1].share == pytest.approx(0.2)
    assert tool_token_shares([_result_row()]) == []
    assert tool_token_shares([_result_row(tool_result_chars={"exec": 0})]) == []


def test_tasks_completed_and_ok_rate():
    from benchmarks.report import tasks_completed

    rows = [
        _result_row(row_id="a", ok=True),
        _result_row(row_id="b", ok=False),
        _result_row(row_id="c", ok=True),
    ]
    stats = tasks_completed(rows)
    assert stats.total == 3
    assert stats.ok == 2
    assert stats.ok_rate == pytest.approx(2 / 3)


def test_report_cli_prints_dashboard(tmp_path, capsys):
    from benchmarks import report

    rows = [
        _result_row(
            row_id="chat-alpha",
            category="chat",
            per_call_inputs=[100, 120, 150],
            cached_tokens=75,
            tool_result_chars={"exec": 400, "get_latest_cve": 100},
        ),
        _result_row(
            row_id="cve-beta",
            category="cve_tool",
            per_call_inputs=[80],
        ),
    ]
    path = tmp_path / "results.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    code = report.main([str(path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "Cache hit rate" in out
    assert "repeated" in out
    assert "exec" in out
    assert "get_latest_cve" in out
    assert "chat-alpha" in out
    assert "cve-beta" in out


def test_report_cli_missing_file_fails(tmp_path, capsys):
    from benchmarks import report

    code = report.main([str(tmp_path / "missing.json")])
    assert code != 0
    captured = capsys.readouterr()
    assert "not found" in captured.err
    assert "missing.json" in captured.err


def test_report_cli_handles_single_turn_rows(tmp_path, capsys):
    from benchmarks import report

    rows = [
        _result_row(row_id="single-one", per_call_inputs=[100]),
        _result_row(row_id="single-two", per_call_inputs=[250]),
    ]
    path = tmp_path / "results.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    code = report.main([str(path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "repeated context share: 0.0%" in out


# --- T7: comparison gate --------------------------------------------------------


def _write_results_file(tmp_path: Path, name: str, rows: list[dict[str, Any]]) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(rows), encoding="utf-8")
    return str(path)


def _run_compare(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> tuple[int, str, str]:
    from benchmarks import compare

    before_path = _write_results_file(tmp_path, "before.json", before)
    after_path = _write_results_file(tmp_path, "after.json", after)
    code = compare.main([before_path, after_path])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_compare_totals_aggregates_rows():
    from benchmarks.compare import totals

    rows = [
        _result_row(
            row_id="t1",
            category="chat",
            per_call_inputs=[100, 60],
            cached_tokens=40,
            cost=0.01,
            tool_calls=3,
        ),
        _result_row(
            row_id="t2",
            category="cve_tool",
            per_call_inputs=[40],
            cost=0.02,
            tool_calls=1,
            ok=False,
        ),
    ]
    agg = totals(rows)
    assert agg.tasks == 2
    assert agg.ok == 1
    assert agg.input_tokens == 200
    assert agg.output_tokens == 9
    assert agg.cached_tokens == 40
    assert agg.cost == pytest.approx(0.03)
    assert agg.turns == 3
    assert agg.tool_calls == 4
    assert agg.ok_rate == pytest.approx(0.5)
    assert agg.cache_hit_rate == pytest.approx(0.2)
    empty = totals([])
    assert empty.tasks == 0
    assert empty.ok_rate == 0.0
    assert empty.cache_hit_rate == 0.0


def test_compute_deltas_total_and_categories():
    from benchmarks.compare import compute_deltas

    before = [
        _result_row(row_id="c1", category="chat", per_call_inputs=[400]),
        _result_row(row_id="c2", category="chat", per_call_inputs=[400]),
        _result_row(row_id="v1", category="cve_tool", per_call_inputs=[300]),
    ]
    after = [
        _result_row(row_id="c1", category="chat", per_call_inputs=[200]),
        _result_row(row_id="c2", category="chat", per_call_inputs=[200]),
        _result_row(row_id="v1", category="cve_tool", per_call_inputs=[150]),
        _result_row(row_id="e1", category="edge", per_call_inputs=[50]),
    ]
    scopes = compute_deltas(before, after)
    assert [scope.label for scope in scopes] == ["Total", "chat", "cve_tool", "edge"]
    by_label = {scope.label: scope for scope in scopes}
    assert by_label["Total"].before.input_tokens == 1100
    assert by_label["Total"].after.input_tokens == 600
    assert by_label["chat"].before.input_tokens == 800
    assert by_label["chat"].after.input_tokens == 400
    assert by_label["chat"].before.tasks == 2
    assert by_label["cve_tool"].after.input_tokens == 150
    assert by_label["edge"].before.input_tokens == 0


def test_evaluate_gate_pure_math():
    from benchmarks.compare import evaluate_gate

    before = [
        _result_row(row_id="b1", per_call_inputs=[500], cost=0.01),
        _result_row(row_id="b2", per_call_inputs=[500], cost=0.01),
    ]
    after = [
        _result_row(row_id="a1", per_call_inputs=[300], cost=0.005),
        _result_row(row_id="a2", per_call_inputs=[300], cost=0.005),
    ]
    result = evaluate_gate(before, after)
    assert result.input_reduction == pytest.approx(0.4)
    assert result.cost_reduction == pytest.approx(0.5)
    assert result.success_drop_pp == pytest.approx(0.0)
    assert result.passed is True
    assert result.reasons == []


def test_evaluate_gate_zero_before_totals_reduce_to_zero():
    from benchmarks.compare import evaluate_gate

    before = [_result_row(row_id="z1", per_call_inputs=[0], cost=0.0)]
    after = [_result_row(row_id="z2", per_call_inputs=[600], cost=0.001)]
    result = evaluate_gate(before, after)
    assert result.input_reduction == 0.0
    assert result.cost_reduction == 0.0
    assert result.passed is False
    assert result.reasons
    empty = evaluate_gate([], [])
    assert empty.input_reduction == 0.0
    assert empty.cost_reduction == 0.0
    assert empty.success_drop_pp == 0.0
    assert empty.passed is False


def test_compare_passes_when_input_reduction_over_30(tmp_path, capsys):
    before = [_result_row(row_id="b1", per_call_inputs=[1000], cost=0.01)]
    after = [_result_row(row_id="a1", per_call_inputs=[600], cost=0.009)]
    code, out, err = _run_compare(tmp_path, capsys, before, after)
    assert code == 0
    assert err == ""
    assert "GATE PASSED" in out
    assert "input tokens: 1000 -> 600" in out


def test_compare_passes_when_cost_reduction_over_30_even_if_tokens_below(
    tmp_path, capsys
):
    before = [_result_row(row_id="b1", per_call_inputs=[1000], cost=0.01)]
    after = [_result_row(row_id="a1", per_call_inputs=[900], cost=0.005)]
    code, out, err = _run_compare(tmp_path, capsys, before, after)
    assert code == 0
    assert err == ""
    assert "GATE PASSED" in out
    assert "input tokens: 1000 -> 900" in out


def test_compare_fails_when_reduction_below_30(tmp_path, capsys):
    before = [_result_row(row_id="b1", per_call_inputs=[1000], cost=0.01)]
    after = [_result_row(row_id="a1", per_call_inputs=[900], cost=0.009)]
    code, out, err = _run_compare(tmp_path, capsys, before, after)
    assert code == 1
    assert "GATE FAILED" in err
    assert "insufficient reduction" in err
    assert "1000 -> 900" in err
    assert "30%" in err
    assert "GATE PASSED" not in out


def test_compare_fails_when_success_drop_over_2pp(tmp_path, capsys):
    before = [
        _result_row(row_id=f"b{i}", per_call_inputs=[100], cost=0.01) for i in range(10)
    ]
    after = [
        _result_row(row_id=f"a{i}", per_call_inputs=[60], cost=0.006, ok=i != 0)
        for i in range(10)
    ]
    code, _, err = _run_compare(tmp_path, capsys, before, after)
    assert code == 1
    assert "success-rate drop" in err
    assert "10.0pp" in err
    assert "2.0pp limit" in err
    assert "100.0% -> 90.0%" in err
    assert "insufficient reduction" not in err


def test_compare_passes_when_success_equal(tmp_path, capsys):
    before = [
        _result_row(row_id=f"b{i}", per_call_inputs=[100], cost=0.01) for i in range(10)
    ]
    after = [
        _result_row(row_id=f"a{i}", per_call_inputs=[60], cost=0.006) for i in range(10)
    ]
    code, out, err = _run_compare(tmp_path, capsys, before, after)
    assert code == 0
    assert err == ""
    assert "success rate: 100.0% -> 100.0%" in out
    assert "GATE PASSED" in out


def test_compare_granularity_one_prompt_flip_fails(tmp_path, capsys):
    before = [
        _result_row(row_id=f"b{i}", per_call_inputs=[100], cost=0.01) for i in range(10)
    ]
    after = [
        _result_row(row_id=f"a{i}", per_call_inputs=[60], cost=0.006, ok=i != 3)
        for i in range(10)
    ]
    code, _, err = _run_compare(tmp_path, capsys, before, after)
    assert code == 1
    assert "success-rate drop" in err
    assert "10.0pp" in err
    assert "insufficient reduction" not in err


def test_compare_per_category_deltas(tmp_path, capsys):
    before = [
        _result_row(row_id="c1", category="chat", per_call_inputs=[400]),
        _result_row(row_id="c2", category="chat", per_call_inputs=[400]),
        _result_row(row_id="v1", category="cve_tool", per_call_inputs=[300]),
        _result_row(row_id="t1", category="tool_chain", per_call_inputs=[200]),
        _result_row(row_id="e1", category="edge", per_call_inputs=[100]),
    ]
    after = [
        _result_row(row_id="c1", category="chat", per_call_inputs=[200]),
        _result_row(row_id="c2", category="chat", per_call_inputs=[200]),
        _result_row(row_id="v1", category="cve_tool", per_call_inputs=[150]),
        _result_row(row_id="t1", category="tool_chain", per_call_inputs=[100]),
        _result_row(row_id="e1", category="edge", per_call_inputs=[50]),
    ]
    code, out, _ = _run_compare(tmp_path, capsys, before, after)
    assert code == 0
    assert "Per-category deltas:" in out
    for category in ("chat", "cve_tool", "tool_chain", "edge"):
        assert f"[{category}]" in out
    assert "input tokens: 1400 -> 700" in out
    assert "input tokens: 800 -> 400" in out
    assert "input tokens: 300 -> 150" in out
    assert "input tokens: 200 -> 100" in out
    assert "input tokens: 100 -> 50" in out


def test_compare_boundary_exact_30pct_reduction_passes(tmp_path, capsys):
    before = [_result_row(row_id="b1", per_call_inputs=[1000], cost=0.01)]
    after = [_result_row(row_id="a1", per_call_inputs=[700], cost=0.0095)]
    code, out, err = _run_compare(tmp_path, capsys, before, after)
    assert code == 0
    assert err == ""
    assert "GATE PASSED" in out
    assert "input tokens: 1000 -> 700" in out


def test_compare_boundary_exact_2pp_drop_passes(tmp_path, capsys):
    before = [
        _result_row(row_id=f"b{i}", per_call_inputs=[20], cost=0.01) for i in range(50)
    ]
    after = [
        _result_row(row_id=f"a{i}", per_call_inputs=[10], cost=0.005, ok=i != 0)
        for i in range(50)
    ]
    code, out, err = _run_compare(tmp_path, capsys, before, after)
    assert code == 0
    assert err == ""
    assert "GATE PASSED" in out


def test_compare_boundary_2_1pp_drop_fails(tmp_path, capsys):
    before = [
        _result_row(row_id=f"b{i}", per_call_inputs=[2], cost=0.01) for i in range(1000)
    ]
    after = [
        _result_row(row_id=f"a{i}", per_call_inputs=[1], cost=0.005, ok=i < 979)
        for i in range(1000)
    ]
    code, _, err = _run_compare(tmp_path, capsys, before, after)
    assert code == 1
    assert "success-rate drop" in err
    assert "2.1pp" in err


def test_compare_missing_file_fails(tmp_path, capsys):
    from benchmarks import compare

    after_path = _write_results_file(tmp_path, "after.json", [_result_row()])
    code = compare.main([str(tmp_path / "missing-before.json"), after_path])
    assert code == 1
    err = capsys.readouterr().err
    assert "not found" in err
    assert "missing-before.json" in err


@pytest.mark.parametrize(
    ("content", "expected_error"),
    [
        ("{not json", "cannot read"),
        ('{"rows": []}', "must contain a JSON list"),
        ("[1, 2]", "must contain a JSON list"),
    ],
)
def test_compare_invalid_file_fails(tmp_path, capsys, content, expected_error):
    from benchmarks import compare

    before_path = tmp_path / "before.json"
    before_path.write_text(content, encoding="utf-8")
    after_path = _write_results_file(tmp_path, "after.json", [_result_row()])
    code = compare.main([str(before_path), after_path])
    assert code == 1
    err = capsys.readouterr().err
    assert expected_error in err
    assert "before.json" in err


def test_compare_handles_zero_before_totals(tmp_path, capsys):
    before = [
        _result_row(row_id="z1", per_call_inputs=[0], cost=0.0),
        _result_row(row_id="z2", per_call_inputs=[0], cost=0.0),
    ]
    after = [_result_row(row_id="a1", per_call_inputs=[600], cost=0.001)]
    code, out, err = _run_compare(tmp_path, capsys, before, after)
    assert code == 1
    assert "insufficient reduction" in err
    assert "input tokens: 0 -> 600" in out
    assert "(n/a)" in out


def test_compare_empty_before_file_fails_gracefully(tmp_path, capsys):
    after = [_result_row(row_id="a1", per_call_inputs=[600], cost=0.001)]
    code, out, err = _run_compare(tmp_path, capsys, [], after)
    assert code == 1
    assert "insufficient reduction" in err
    assert "success-rate drop" not in err
    assert "Total: 0 -> 1 tasks" in out

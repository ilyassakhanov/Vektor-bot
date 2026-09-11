"""Benchmark runner — measures token usage, latency, and tool calls per prompt.

Runs the prompts in ``benchmarks/prompts.json`` through the full Agent loop
against a real Ollama instance and writes per-prompt JSON results for
before/after optimization comparison. Never runs during normal pytest —
invoke manually with ``python -m benchmarks.run``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, cast

import httpx

from agent.agent import MAX_ITERATIONS_REPLY, Agent
from llm.base import (
    LLM,
    ChatResponse,
    LLMResponse,
    Message,
    TokenUsage,
    ToolSpec,
)
from llm.cost import estimate_cost
from llm.ollama import OllamaLLM
from skills.loader import SkillLoader
from tools.cve import CveTool
from tools.exec import ExecTool
from tools.registry import ToolRegistry

log = logging.getLogger("vektor.benchmarks")

_BENCHMARKS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _BENCHMARKS_DIR.parent
_DEFAULT_PROMPTS = _BENCHMARKS_DIR / "prompts.json"
_DEFAULT_OUTPUT = _BENCHMARKS_DIR / "results.json"
_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "llama3.2"
_TOOL_TIMEOUT = 30.0
_PROBE_TIMEOUT = 2.0


class CountingLLM(LLM):
    """LLM wrapper that accumulates token usage and call counts.

    The Agent only surfaces the final text answer, so per-prompt token usage
    is captured here by delegating every ``chat``/``generate`` call to the
    wrapped LLM, summing the reported :class:`TokenUsage`, and recording the
    ordered per-call usage sequence in ``per_call``.
    """

    def __init__(self, inner: LLM) -> None:
        self._inner = inner
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.calls = 0
        self.per_call: list[TokenUsage] = []

    def reset(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.calls = 0
        self.per_call = []

    def generate(self, message: str) -> LLMResponse:
        response = self._inner.generate(message)
        self.calls += 1
        self._accumulate(response.usage)
        return response

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system: str = "",
    ) -> ChatResponse:
        response = self._inner.chat(messages, tools, system=system)
        self.calls += 1
        self._accumulate(response.usage)
        return response

    def _accumulate(self, usage: TokenUsage | None) -> None:
        if usage is not None:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            self.cached_tokens += usage.cached_tokens
        self.per_call.append(usage if usage is not None else TokenUsage())


class CountingRegistry(ToolRegistry):
    """ToolRegistry that counts tool executions and result sizes."""

    def __init__(self) -> None:
        super().__init__()
        self.tool_calls = 0
        self.result_chars: dict[str, int] = {}

    def reset(self) -> None:
        self.tool_calls = 0
        self.result_chars = {}

    def execute(self, name: str, **arguments: Any) -> str:
        self.tool_calls += 1
        result = super().execute(name, **arguments)
        self.result_chars[name] = self.result_chars.get(name, 0) + len(result)
        return result


def load_prompts(path: Path) -> list[dict[str, str]]:
    """Read and validate the prompts manifest (JSON list of prompt entries)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise TypeError(f"Prompts file must contain a JSON list: {path}")
    prompts: list[dict[str, str]] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise TypeError(f"Prompt entry must be an object: {entry!r}")
        candidate = {
            "id": entry.get("id"),
            "prompt": entry.get("prompt"),
            "category": entry.get("category"),
        }
        if not all(isinstance(v, str) and v for v in candidate.values()):
            raise ValueError(
                f"Prompt entry needs non-empty string id/prompt/category: {entry!r}"
            )
        prompts.append(cast(dict[str, str], candidate))
    return prompts


def build_benchmark_agent(
    base_url: str,
    model: str,
) -> tuple[Agent, CountingLLM, CountingRegistry]:
    """Build the benchmark Agent with raw OllamaLLM and counting wrappers."""
    ollama = OllamaLLM(base_url=base_url, model=model)
    counting = CountingLLM(ollama)
    registry = CountingRegistry()
    registry.register(ExecTool(timeout=_TOOL_TIMEOUT))
    registry.register(CveTool(timeout=_TOOL_TIMEOUT))
    loader = SkillLoader(_PROJECT_ROOT / "skills")
    agent = Agent(
        llm=counting,
        tools=registry,
        system_prompt=loader.system_prompt(),
    )
    return agent, counting, registry


def run_prompt(
    agent: Agent,
    counting: CountingLLM,
    registry: CountingRegistry,
    entry: dict[str, str],
    max_iterations: int = 8,
) -> dict[str, Any]:
    """Run one prompt through the agent loop and collect its measurements.

    ``max_iterations`` must match the agent's own limit so the
    max-iterations fallback reply is detected correctly.
    """
    counting.reset()
    registry.reset()
    start = time.perf_counter()
    reply = agent.run(entry["prompt"])
    elapsed = time.perf_counter() - start
    fallback = MAX_ITERATIONS_REPLY.format(max_iterations=max_iterations)
    if not reply:
        ok = False
        failure = "empty_reply"
    elif reply == fallback:
        ok = False
        failure = "max_iterations"
    else:
        ok = True
        failure = ""
    per_call = [
        {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cached_tokens": usage.cached_tokens,
            "latency_ns": usage.latency_ns,
            "model": usage.model,
        }
        for usage in counting.per_call
    ]
    return {
        "id": entry["id"],
        "category": entry["category"],
        "prompt": entry["prompt"],
        "reply_len": len(reply),
        "input_tokens": counting.input_tokens,
        "output_tokens": counting.output_tokens,
        "cached_tokens": counting.cached_tokens,
        "llm_calls": counting.calls,
        "tool_calls": registry.tool_calls,
        "latency_s": round(elapsed, 4),
        "ok": ok,
        "failure": failure,
        "cost": sum(estimate_cost(usage) for usage in counting.per_call),
        "turns": counting.calls,
        "per_call": per_call,
        "tool_result_chars": dict(registry.result_chars),
    }


def write_results(results: list[dict[str, Any]], path: Path) -> None:
    """Write benchmark results to ``path`` as indented JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
        f.write("\n")


def ollama_reachable(base_url: str) -> bool:
    """Probe the Ollama instance — True when it answers with a sane status."""
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/api/tags",
            timeout=_PROBE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        log.info("Ollama probe failed for %s: %s", base_url, exc)
        return False
    return resp.status_code < 500


def _print_summary(results: list[dict[str, Any]], output: Path) -> None:
    total_input = int(sum(r["input_tokens"] for r in results))
    total_output = int(sum(r["output_tokens"] for r in results))
    total_tool_calls = int(sum(r["tool_calls"] for r in results))
    mean_latency = (
        float(sum(r["latency_s"] for r in results) / len(results)) if results else 0.0
    )
    print(
        f"Benchmarks complete: {len(results)} prompts, "
        f"{total_input} input tokens, {total_output} output tokens, "
        f"{total_tool_calls} tool calls, mean latency {mean_latency:.2f}s"
    )
    print(f"Results written to {output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark the Vektor agent against a real Ollama instance."
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OLLAMA_BASE_URL", _DEFAULT_BASE_URL),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OLLAMA_MODEL", _DEFAULT_MODEL),
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--prompts", type=Path, default=_DEFAULT_PROMPTS)
    args = parser.parse_args(argv)

    if not ollama_reachable(args.base_url):
        print(f"Skipping benchmarks: Ollama not reachable at {args.base_url}")
        return 0

    prompts = load_prompts(args.prompts)
    agent, counting, registry = build_benchmark_agent(args.base_url, args.model)
    results: list[dict[str, Any]] = []
    for entry in prompts:
        log.info("running prompt %s (%s)", entry["id"], entry["category"])
        result = run_prompt(agent, counting, registry, entry)
        results.append(result)
        print(
            f"[{result['id']}] latency={result['latency_s']}s "
            f"llm_calls={result['llm_calls']} tool_calls={result['tool_calls']} "
            f"input_tokens={result['input_tokens']} "
            f"output_tokens={result['output_tokens']}"
        )
    write_results(results, args.output)
    _print_summary(results, args.output)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    raise SystemExit(main())

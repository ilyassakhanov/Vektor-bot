"""Token-cost audit report for benchmark result files.

Reads a results JSON written by ``benchmarks.run`` (via the file only —
this module never imports or invokes the runner) and prints a dashboard:
tasks completed with success rate, total input/output/cached tokens,
estimated cost, per-task averages, cache hit rate, repeated-context
share, most expensive tools by token share, and a per-task timeline.
Only aggregate numbers, tool names, and task ids/categories are printed
— never prompt text.

Documented approximations:

- **Repeated context** assumes a stable prompt prefix and an append-only
  conversation history: every call after the first re-sends the previous
  call's full input and appends only new tokens. ``repeated`` sums each
  call's predecessor input; ``new`` is the context growth after the first
  call (final minus first input, clamped at zero). Tool results appended
  mid-run make that growth an upper bound on user-added tokens. A
  single-call task has no re-sends; its entire input counts as new.
- **Per-tool tokens** are estimated as ``chars / 4`` (four characters per
  token) from the recorded tool-result character counts; each tool's
  share is its fraction of total recorded tool-result characters.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, NamedTuple

log = logging.getLogger("vektor.benchmarks.report")

_CHARS_PER_TOKEN = 4


class RepeatedContext(NamedTuple):
    """Repeated/new/total input-token volumes across all tasks.

    ``share`` is the fraction of all input tokens that was re-sent
    context (0.0 when nothing was sent).
    """

    repeated: int
    new: int
    total: int

    @property
    def share(self) -> float:
        if self.total == 0:
            return 0.0
        return self.repeated / self.total


class ToolShare(NamedTuple):
    """Estimated token volume and share of one tool's results."""

    name: str
    tokens: int
    share: float


class TaskStats(NamedTuple):
    """Task counts and success rate of a results file."""

    total: int
    ok: int
    ok_rate: float


def _row_int(row: dict[str, Any], key: str) -> int:
    return int(row.get(key, 0))


def _per_call_inputs(row: dict[str, Any]) -> list[int]:
    per_call = row.get("per_call") or []
    return [int(call.get("input_tokens", 0)) for call in per_call]


def cache_hit_rate(rows: list[dict[str, Any]]) -> float:
    """Return the cache hit rate: Σcached / Σinput (0.0 when no input)."""
    total_input = sum(_row_int(row, "input_tokens") for row in rows)
    total_cached = sum(_row_int(row, "cached_tokens") for row in rows)
    if total_input == 0:
        return 0.0
    return total_cached / total_input


def repeated_context(rows: list[dict[str, Any]]) -> RepeatedContext:
    """Return repeated/new/total input-token volumes across all tasks.

    Approximation (see module docstring): with a stable prompt prefix and
    append-only history, every call after the first re-sends its
    predecessor's full input. Per task with per-call inputs ``[i1..iN]``:
    ``repeated`` gains ``Σ i[n-1]`` for ``n ≥ 2``; ``new`` gains
    ``iN − i1`` (clamped at zero) for multi-call tasks, or the full
    ``i1`` for a single-call task (nothing re-sent, all new); ``total``
    is the summed row-level input tokens.
    """
    repeated = 0
    new = 0
    total = sum(_row_int(row, "input_tokens") for row in rows)
    for row in rows:
        inputs = _per_call_inputs(row)
        if len(inputs) <= 1:
            if inputs:
                new += inputs[0]
            continue
        repeated += sum(inputs[:-1])
        new += max(inputs[-1] - inputs[0], 0)
    return RepeatedContext(repeated=repeated, new=new, total=total)


def tool_token_shares(rows: list[dict[str, Any]]) -> list[ToolShare]:
    """Return per-tool token shares, sorted by descending token volume.

    Tokens are approximated as ``chars / 4`` from the summed
    ``tool_result_chars``; each share is the tool's fraction of total
    recorded tool-result characters. Tools with equal token volumes are
    ordered by name for determinism. Returns an empty list when no
    tool-result characters were recorded (or all were zero).
    """
    chars: dict[str, int] = {}
    for row in rows:
        recorded = row.get("tool_result_chars") or {}
        for name, count in recorded.items():
            chars[str(name)] = chars.get(str(name), 0) + int(count)
    total_chars = sum(chars.values())
    if total_chars == 0:
        return []
    shares = [
        ToolShare(
            name=name,
            tokens=value // _CHARS_PER_TOKEN,
            share=value / total_chars,
        )
        for name, value in chars.items()
    ]
    shares.sort(key=lambda share: (-share.tokens, share.name))
    return shares


def tasks_completed(rows: list[dict[str, Any]]) -> TaskStats:
    """Return task count, successful-task count, and success rate."""
    total = len(rows)
    ok = sum(1 for row in rows if row.get("ok"))
    ok_rate = ok / total if total else 0.0
    return TaskStats(total=total, ok=ok, ok_rate=ok_rate)


def _task_timeline(row: dict[str, Any]) -> list[str]:
    """Return the timeline lines for one task (ids/numbers only)."""
    row_id = str(row.get("id", "?"))
    category = str(row.get("category", "?"))
    failure = str(row.get("failure") or "unknown")
    status = "ok" if row.get("ok") else f"failed ({failure})"
    lines = [
        (
            f"[{row_id}] ({category}) {status} - "
            f"{_row_int(row, 'turns')} turns, {_row_int(row, 'tool_calls')} tool calls"
        )
    ]
    per_call = row.get("per_call") or []
    for index, call in enumerate(per_call, start=1):
        lines.append(
            f"    turn {index}: input={int(call.get('input_tokens', 0))}, "
            f"output={int(call.get('output_tokens', 0))}, "
            f"cached={int(call.get('cached_tokens', 0))}"
        )
    return lines


def format_dashboard(rows: list[dict[str, Any]]) -> str:
    """Return the full audit dashboard text for the given result rows."""
    stats = tasks_completed(rows)
    total_input = sum(_row_int(row, "input_tokens") for row in rows)
    total_output = sum(_row_int(row, "output_tokens") for row in rows)
    total_cached = sum(_row_int(row, "cached_tokens") for row in rows)
    total_cost = sum(float(row.get("cost", 0.0)) for row in rows)
    total_turns = sum(_row_int(row, "turns") for row in rows)
    total_tool_calls = sum(_row_int(row, "tool_calls") for row in rows)
    tasks = stats.total if stats.total else 1
    context = repeated_context(rows)
    lines = [
        f"Tasks completed: {stats.total} ({stats.ok} ok, {stats.ok_rate:.1%} success rate)",
        f"Total tokens: {total_input} input / {total_output} output / {total_cached} cached",
        f"Estimated cost: ${total_cost:.6f}",
        (
            f"Average per task: {total_input / tasks:.1f} input tokens, "
            f"{total_output / tasks:.1f} output tokens, "
            f"{total_turns / tasks:.1f} turns, {total_tool_calls / tasks:.1f} tool calls"
        ),
        f"Cache hit rate: {cache_hit_rate(rows):.1%}",
        (
            f"repeated context share: {context.share:.1%} of input re-sent "
            f"({context.repeated} repeated, {context.new} new, {context.total} total)"
        ),
        "Most expensive tools (estimated tokens, chars / 4):",
    ]
    shares = tool_token_shares(rows)
    if shares:
        lines.extend(
            f"  {share.name}: {share.tokens} tokens ({share.share:.1%})"
            for share in shares
        )
    else:
        lines.append("  (no tool results recorded)")
    lines.append("Per-task timeline:")
    for row in rows:
        lines.extend(_task_timeline(row))
    return "\n".join(lines)


def _load_rows(path: Path) -> list[dict[str, Any]] | None:
    """Load result rows from ``path``; print an error and return None on failure."""
    if not path.is_file():
        print(f"error: results file not found: {path}", file=sys.stderr)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"error: cannot read results file {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, list):
        print(f"error: results file must contain a JSON list: {path}", file=sys.stderr)
        return None
    return data


def main(argv: list[str] | None = None) -> int:
    """Print the audit dashboard for a results file; 0 on success, 1 on error."""
    parser = argparse.ArgumentParser(
        description="Print a token-cost audit dashboard from benchmark results."
    )
    parser.add_argument(
        "results", type=Path, help="path to a benchmark results JSON file"
    )
    args = parser.parse_args(argv)
    rows = _load_rows(args.results)
    if rows is None:
        return 1
    log.info("reporting %d result rows from %s", len(rows), args.results)
    print(format_dashboard(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

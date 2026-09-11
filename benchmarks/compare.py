"""Before/after comparison gate for benchmark result files.

Reads two results JSON files written by ``benchmarks.run`` (via the
files only — this module never imports or invokes the runner; it reuses
the pure math helpers from ``benchmarks.report``) and prints
per-category and total deltas: input/output/cached tokens, cost, turns,
tool calls, success rate, and cache hit rate. Only aggregate numbers
and category names are printed — never prompt text.

Gate rule (exit status 1 on failure):

- ``reduction = 1 - after / before`` for total input tokens and for
  total cost; a zero before-total reports 0.0 (an empty baseline proves
  no reduction) instead of dividing by zero;
- ``success drop (pp) = (before ok rate - after ok rate) * 100``;
- the gate fails when ``(input reduction < 30% AND cost reduction
  < 30%)`` OR ``success drop > 2.0pp`` — the disjunction matches the
  spec's "input tokens (or estimated cost)" requirement.

Threshold comparisons round measured values to 9 decimal places so
exact boundary cases (an exactly-30% reduction, an exactly-2pp drop)
stay on the passing side despite float representation noise.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, NamedTuple

from benchmarks.report import tasks_completed

log = logging.getLogger("vektor.benchmarks.compare")

_REDUCTION_THRESHOLD = 0.30
_SUCCESS_DROP_LIMIT_PP = 2.0
_BOUNDARY_PRECISION = 9


class Totals(NamedTuple):
    """Aggregated task counts and volumes for one scope of result rows.

    ``ok_rate`` and ``cache_hit_rate`` guard zero denominators (0.0 when
    there are no tasks / no input tokens), mirroring the
    ``benchmarks.report`` helpers.
    """

    tasks: int
    ok: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost: float
    turns: int
    tool_calls: int

    @property
    def ok_rate(self) -> float:
        if self.tasks == 0:
            return 0.0
        return self.ok / self.tasks

    @property
    def cache_hit_rate(self) -> float:
        if self.input_tokens == 0:
            return 0.0
        return self.cached_tokens / self.input_tokens


class ScopeDeltas(NamedTuple):
    """Before/after totals for one scope: the total or one category."""

    label: str
    before: Totals
    after: Totals


class GateResult(NamedTuple):
    """Measured gate quantities with the pass verdict and failure reasons.

    Each reason states which condition failed and the measured values.
    """

    input_reduction: float
    cost_reduction: float
    success_drop_pp: float
    passed: bool
    reasons: list[str]


def _row_int(row: dict[str, Any], key: str) -> int:
    return int(row.get(key, 0))


def totals(rows: list[dict[str, Any]]) -> Totals:
    """Aggregate task counts, token volumes, cost, turns, and tool calls."""
    stats = tasks_completed(rows)
    return Totals(
        tasks=stats.total,
        ok=stats.ok,
        input_tokens=sum(_row_int(row, "input_tokens") for row in rows),
        output_tokens=sum(_row_int(row, "output_tokens") for row in rows),
        cached_tokens=sum(_row_int(row, "cached_tokens") for row in rows),
        cost=sum(float(row.get("cost", 0.0)) for row in rows),
        turns=sum(_row_int(row, "turns") for row in rows),
        tool_calls=sum(_row_int(row, "tool_calls") for row in rows),
    )


def reduction(before: float, after: float) -> float:
    """Return ``1 - after / before``, guarding a zero denominator.

    A zero (or negative) before-total reports 0.0: an empty baseline
    proves no reduction, so it fails the >= 30% requirement rather than
    dividing by zero.
    """
    if before <= 0:
        return 0.0
    return 1 - after / before


def _clean(value: float) -> float:
    """Round away float representation noise before threshold comparisons."""
    return round(value, _BOUNDARY_PRECISION)


def evaluate_gate(
    before_rows: list[dict[str, Any]],
    after_rows: list[dict[str, Any]],
) -> GateResult:
    """Evaluate the comparison gate; the rule is documented module-wide.

    Returns the measured input/cost reductions, the success-rate drop in
    percentage points, the pass verdict, and one human-readable reason
    per failed condition.
    """
    before = totals(before_rows)
    after = totals(after_rows)
    input_reduction = reduction(before.input_tokens, after.input_tokens)
    cost_reduction = reduction(before.cost, after.cost)
    success_drop_pp = (before.ok_rate - after.ok_rate) * 100
    reasons: list[str] = []
    if (
        _clean(input_reduction) < _REDUCTION_THRESHOLD
        and _clean(cost_reduction) < _REDUCTION_THRESHOLD
    ):
        reasons.append(
            f"insufficient reduction: input tokens {before.input_tokens} -> "
            f"{after.input_tokens} ({input_reduction:.1%} reduction) and cost "
            f"${before.cost:.6f} -> ${after.cost:.6f} ({cost_reduction:.1%} "
            f"reduction) are both below the {_REDUCTION_THRESHOLD:.0%} threshold"
        )
    if _clean(success_drop_pp) > _SUCCESS_DROP_LIMIT_PP:
        reasons.append(
            f"success-rate drop {success_drop_pp:.1f}pp exceeds the "
            f"{_SUCCESS_DROP_LIMIT_PP:.1f}pp limit "
            f"({before.ok_rate:.1%} -> {after.ok_rate:.1%})"
        )
    return GateResult(
        input_reduction=input_reduction,
        cost_reduction=cost_reduction,
        success_drop_pp=success_drop_pp,
        passed=not reasons,
        reasons=reasons,
    )


def _category_of(row: dict[str, Any]) -> str:
    return str(row.get("category", "?"))


def compute_deltas(
    before_rows: list[dict[str, Any]],
    after_rows: list[dict[str, Any]],
) -> list[ScopeDeltas]:
    """Return before/after totals for the total scope and each category.

    The total scope comes first, then one scope per category name in
    sorted order; a category present in only one of the two files still
    gets a scope (the missing side totals zero).
    """
    scopes = [ScopeDeltas("Total", totals(before_rows), totals(after_rows))]
    categories = sorted(
        {_category_of(row) for row in before_rows}
        | {_category_of(row) for row in after_rows}
    )
    for category in categories:
        before_cat = [row for row in before_rows if _category_of(row) == category]
        after_cat = [row for row in after_rows if _category_of(row) == category]
        scopes.append(ScopeDeltas(category, totals(before_cat), totals(after_cat)))
    return scopes


def _change(before: float, after: float) -> str:
    """Return the signed relative change, or n/a when before is zero."""
    if before == 0:
        return "(n/a)"
    return f"({(after - before) / before * 100:+.1f}%)"


def _pp_change(before: float, after: float) -> str:
    """Return the signed percentage-point change between two rates."""
    return f"({(after - before) * 100:+.1f}pp)"


def _format_scope(title: str, scope: ScopeDeltas) -> list[str]:
    before = scope.before
    after = scope.after
    return [
        f"{title}: {before.tasks} -> {after.tasks} tasks",
        (
            f"  input tokens: {before.input_tokens} -> {after.input_tokens} "
            f"{_change(before.input_tokens, after.input_tokens)}"
        ),
        (
            f"  output tokens: {before.output_tokens} -> {after.output_tokens} "
            f"{_change(before.output_tokens, after.output_tokens)}"
        ),
        (
            f"  cached tokens: {before.cached_tokens} -> {after.cached_tokens} "
            f"{_change(before.cached_tokens, after.cached_tokens)}"
        ),
        (
            f"  cost: ${before.cost:.6f} -> ${after.cost:.6f} "
            f"{_change(before.cost, after.cost)}"
        ),
        (
            f"  turns: {before.turns} -> {after.turns} "
            f"{_change(before.turns, after.turns)}"
        ),
        (
            f"  tool calls: {before.tool_calls} -> {after.tool_calls} "
            f"{_change(before.tool_calls, after.tool_calls)}"
        ),
        (
            f"  success rate: {before.ok_rate:.1%} -> {after.ok_rate:.1%} "
            f"{_pp_change(before.ok_rate, after.ok_rate)}"
        ),
        (
            f"  cache hit rate: {before.cache_hit_rate:.1%} -> "
            f"{after.cache_hit_rate:.1%} "
            f"{_pp_change(before.cache_hit_rate, after.cache_hit_rate)}"
        ),
    ]


def format_deltas(
    before_rows: list[dict[str, Any]],
    after_rows: list[dict[str, Any]],
) -> str:
    """Return the total and per-category delta table (pure, no printing)."""
    scopes = compute_deltas(before_rows, after_rows)
    lines = _format_scope(scopes[0].label, scopes[0])
    if len(scopes) > 1:
        lines.append("Per-category deltas:")
        for scope in scopes[1:]:
            lines.extend(_format_scope(f"[{scope.label}]", scope))
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
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        print(
            f"error: results file must contain a JSON list of objects: {path}",
            file=sys.stderr,
        )
        return None
    return data


def main(argv: list[str] | None = None) -> int:
    """Print before/after deltas and apply the comparison gate.

    Returns 0 when the gate passes, 1 when it fails or a file cannot be
    loaded. The delta table goes to stdout; failure reasons go to stderr
    and state which condition failed with the measured values.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Compare before/after benchmark results; exits non-zero when "
            "input and cost reductions are both below 30% or the success-rate "
            "drop exceeds 2 percentage points."
        )
    )
    parser.add_argument("before", type=Path, help="path to the BEFORE results JSON")
    parser.add_argument("after", type=Path, help="path to the AFTER results JSON")
    args = parser.parse_args(argv)
    before_rows = _load_rows(args.before)
    if before_rows is None:
        return 1
    after_rows = _load_rows(args.after)
    if after_rows is None:
        return 1
    log.info(
        "comparing %s (%d rows) vs %s (%d rows)",
        args.before,
        len(before_rows),
        args.after,
        len(after_rows),
    )
    print(
        f"Comparing {args.before} ({len(before_rows)} rows) vs "
        f"{args.after} ({len(after_rows)} rows)"
    )
    print(format_deltas(before_rows, after_rows))
    result = evaluate_gate(before_rows, after_rows)
    if result.passed:
        print(
            f"GATE PASSED: input reduction {result.input_reduction:.1%}, "
            f"cost reduction {result.cost_reduction:.1%}, success-rate drop "
            f"{result.success_drop_pp:.1f}pp"
        )
        return 0
    print("GATE FAILED:", file=sys.stderr)
    for reason in result.reasons:
        print(f"  - {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

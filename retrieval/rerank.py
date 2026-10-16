"""Pointwise LLM reranking of fused retrieval hits — best-effort, never fails.

One LLM call scores every hit 0-10; tolerant reply parsing (think tags
stripped; JSON object/array or plain numbers; clamped [0, 10]; missing → 0).
Every failure path returns the input (RRF) order with ``ok=False``.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from llm.base import LLM
from retrieval.rrf import FusedHit

log = logging.getLogger("vektor.retrieval.rerank")

_MAX_PASSAGE_CHARS = 600

_PROMPT_TEMPLATE = (
    "Rate how relevant each numbered passage is to the query on a 0-10 scale"
    " (10 = fully relevant).\n"
    "Reply with ONLY this minimal JSON, no explanations:\n"
    '{{"scores": [<passage 1 score>, <passage 2 score>, ...]}}\n'
    "Query: {query}\n"
    "{passages}"
)


@dataclass(frozen=True)
class RerankResult:
    """Outcome of one rerank pass.

    ``ok`` is True when the model produced at least one usable score and
    the hits were re-scored; False means the input (RRF) order was kept.
    """

    hits: list[FusedHit]
    ok: bool


class Reranker:
    """Pointwise reranker: one LLM call scoring all hits; never raises."""

    def __init__(self, llm: LLM) -> None:
        self._llm = llm

    def rerank(self, query: str, hits: list[FusedHit], top_k: int) -> RerankResult:
        """Re-order ``hits`` by LLM relevance scores; keep ``top_k``.

        Never raises: any failure returns the input order truncated to
        ``top_k`` with ``ok=False``; ties keep the input order.
        """
        if not hits:
            return RerankResult(hits=[], ok=False)
        try:
            response = self._llm.generate(_build_prompt(query, hits))
            scores = _parse_scores(response.text, len(hits))
        except Exception:
            log.debug("rerank call failed; keeping RRF order", exc_info=True)
            return RerankResult(hits=hits[:top_k], ok=False)
        ranked = [hit for _, hit in sorted(enumerate(hits), key=_by_score(scores))]
        return RerankResult(hits=ranked[:top_k], ok=True)

    def close(self) -> None:
        """Release the rerank LLM's resources (no-op for LLMs without close)."""
        close = getattr(self._llm, "close", None)
        if callable(close):
            close()


def _by_score(scores: list[float]) -> Any:
    """Sort key over ``(position, hit)`` pairs: descending relevance score."""

    def key(pair: tuple[int, FusedHit]) -> float:
        return -scores[pair[0]]

    return key


def _build_prompt(query: str, hits: list[FusedHit]) -> str:
    """Render the scoring prompt: query plus numbered trimmed passages."""
    passages = "\n".join(
        f"Passage {position}: {' '.join(hit.content.split())[:_MAX_PASSAGE_CHARS]}"
        for position, hit in enumerate(hits, start=1)
    )
    return _PROMPT_TEMPLATE.format(query=query, passages=passages)


def _parse_scores(text: str, count: int) -> list[float]:
    """Parse ``count`` relevance scores from a model reply (tolerant, aligned).

    Accepts JSON ``{"scores": [...]}``, a bare array, or plain numbers;
    missing/invalid → 0; raises ValueError when nothing usable remains.
    """
    payload = _extract_json_object(text)
    if payload is not None:
        numbers = _numeric_items(payload.get("scores"))
    elif (extracted := _extract_json_array(text)) is not None:
        numbers = _numeric_items(extracted)
    elif "{" in text or "}" in text:
        raise ValueError("json-shaped reply does not parse")
    else:
        numbers = _positional_numbers(re.split(r"[,\n]", text))
    if not any(number is not None for number in numbers):
        raise ValueError("no usable scores in reply")
    return _normalized(numbers, count)


def _strip_think(text: str) -> str:
    """Drop ``<think>`` reasoning: keep text after a closed tag, or before
    an unclosed one."""
    if "</think>" in text:
        return text.rsplit("</think>", 1)[-1]
    start = text.find("<think>")
    if start != -1:
        return text[:start]
    return text


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the first JSON object embedded in ``text``, or None."""
    body = _strip_think(text)
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(body[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_json_array(text: str) -> list[Any] | None:
    """Return the first JSON array embedded in ``text``, or None."""
    body = _strip_think(text)
    start = body.find("[")
    end = body.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(body[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, list) else None


def _numeric_items(value: Any) -> list[float | None]:
    """Positionally map a payload list to floats; non-numbers become None."""
    if not isinstance(value, list):
        return []
    return [
        None
        if isinstance(item, bool) or not isinstance(item, (int, float))
        else float(item)
        for item in value
    ]


def _positional_numbers(tokens: list[str]) -> list[float | None]:
    """Positionally map raw tokens to floats; unparseable tokens become None."""
    numbers: list[float | None] = []
    for token in tokens:
        try:
            numbers.append(float(token.strip()))
        except ValueError:
            numbers.append(None)
    return numbers


def _normalized(numbers: list[float | None], count: int) -> list[float]:
    """Pad/truncate to ``count`` entries; clamp finite values into [0, 10]."""
    scores: list[float] = []
    for index in range(count):
        value = numbers[index] if index < len(numbers) else None
        if value is None or not math.isfinite(value):
            scores.append(0.0)
        else:
            scores.append(min(max(value, 0.0), 10.0))
    return scores

"""Query expansion via a small LLM — best-effort, never fails retrieval.

QueryExpander asks a small model (configured by the caller with temperature 0
and a short timeout) for keywords and alternative phrasings of a search query.
The reply is parsed as minimal JSON (``{"keywords": [...], "queries": [...]}``)
with a comma/newline-separated fallback. Every failure path — LLMError,
unexpected exception, empty/whitespace text, unparseable output, or junk-only
tokens — yields the original query with ``used_expansion=False``; :meth:`expand`
never raises, so expansion can never fail retrieval.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from llm.base import LLM

log = logging.getLogger("vektor.retrieval.expansion")

_MAX_KEYWORDS = 8
_MAX_ALT_QUERIES = 3

_PROMPT_TEMPLATE = (
    "Expand the search query below with keywords and alternative phrasings.\n"
    "Reply with ONLY this minimal JSON, no explanations:\n"
    '{{"keywords": ["k1", "k2"], "queries": ["alt query 1", "alt query 2"]}}\n'
    "Query: {query}"
)


@dataclass(frozen=True)
class ExpandedQuery:
    """Result of query expansion.

    ``used_expansion`` is True only when the small model contributed at least
    one usable keyword or alternative query.
    """

    original: str
    keywords: tuple[str, ...]
    alt_queries: tuple[str, ...]
    used_expansion: bool


class QueryExpander:
    """Expands a query via ``LLM.generate()``; degrades to the original query."""

    def __init__(self, llm: LLM) -> None:
        self._llm = llm

    def expand(self, query: str) -> ExpandedQuery:
        """Expand ``query`` into keywords + alternative queries.

        This method never raises: any failure (LLMError, unexpected exception,
        empty or unparseable model output, junk-only tokens) returns the
        original query with ``used_expansion=False`` so retrieval always
        proceeds.
        """
        fallback = ExpandedQuery(
            original=query, keywords=(), alt_queries=(), used_expansion=False
        )
        try:
            response = self._llm.generate(_PROMPT_TEMPLATE.format(query=query))
            raw_keywords, raw_queries = _parse_reply(response.text)
        except Exception:
            log.debug(
                "query expansion call failed; falling back to original query",
                exc_info=True,
            )
            return fallback
        keywords = _sanitize(raw_keywords, query, _MAX_KEYWORDS)
        alt_queries = _sanitize(raw_queries, query, _MAX_ALT_QUERIES)
        if not keywords and not alt_queries:
            log.debug(
                "query expansion produced no usable terms; keeping original query"
            )
            return fallback
        return ExpandedQuery(
            original=query,
            keywords=keywords,
            alt_queries=alt_queries,
            used_expansion=True,
        )


def _parse_reply(text: str) -> tuple[list[str], list[str]]:
    """Parse model output as (keywords, alt_queries).

    Tries a JSON object first (robustly extracted between the first ``{`` and
    the last ``}``); falls back to comma/newline-separated tokens of the raw
    text.
    """
    payload = _extract_json_object(text)
    if payload is not None:
        return _string_items(payload.get("keywords")), _string_items(
            payload.get("queries")
        )
    return _split_tokens(text), []


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the first JSON object embedded in ``text``, or None."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _string_items(value: Any) -> list[str]:
    """Defensively extract string items from a list value; else empty."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _split_tokens(text: str) -> list[str]:
    """Split raw text on commas and newlines (fallback parsing)."""
    return re.split(r"[,\n]", text)


def _sanitize(tokens: list[str], original: str, cap: int) -> tuple[str, ...]:
    """Strip, dedupe case-insensitively (first casing wins), drop the original
    query itself, and cap the result at ``cap`` tokens."""
    seen: set[str] = set()
    original_folded = original.strip().casefold()
    kept: list[str] = []
    for token in tokens:
        cleaned = token.strip()
        if not cleaned:
            continue
        folded = cleaned.casefold()
        if folded in seen or folded == original_folded:
            continue
        seen.add(folded)
        kept.append(cleaned)
        if len(kept) == cap:
            break
    return tuple(kept)

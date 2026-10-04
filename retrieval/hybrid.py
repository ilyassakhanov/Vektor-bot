"""Hybrid retrieval — orchestrates expansion, parallel search, and RRF fusion.

HybridRetriever is the composition point of the retrieval pipeline:

    query → expansion (optional, never fails)
          → embed original + alt queries in one batch
          → ThreadPoolExecutor[max_workers=2]( vector ‖ FTS )
          → rrf_fuse → top-k

The two searchable backends are hidden behind the thin :class:`VectorSearch`
and :class:`FtsSearch` ABCs so neither numpy nor SQLite leaks into this
module; concrete implementations (VectorIndex, ChunkStore adapters) are
wired by callers. Every stage degrades instead of failing: a source that
raises (or an embedder that cannot produce query vectors) simply
contributes no ranking, the other source still answers, and the degradation
is recorded in :attr:`HybridResult.note`. Only when no source produces any
ranking does the result come back empty — still with an explanatory note.

The FTS terms are built from the original query plus expansion keywords and
alternative queries (deduplicated, order-preserving). The vector source
searches with embeddings of the original query plus the alternative queries
(one batch embed call, same dedup) — VectorIndex scores each chunk by max
cosine across them. Expansion KEYWORDS stay FTS-side only: they are BM25
terms, not sentences, so they are never embedded. Sensitive text (query,
terms, hit content) never reaches logs.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from metrics import (
    retrieval_expansion_total,
    retrieval_latency_seconds,
    retrieval_results,
)
from retrieval.embeddings import Embedder, EmbeddingError
from retrieval.expansion import ExpandedQuery, QueryExpander
from retrieval.rrf import ChunkHit, FusedHit, rrf_fuse

log = logging.getLogger("vektor.retrieval.hybrid")


class FtsSearch(ABC):
    """Thin full-text search interface — one term-based ranking method."""

    @abstractmethod
    def search(self, terms: list[str], limit: int) -> list[ChunkHit]:
        """Return up to ``limit`` best-first hits matching ``terms``."""


class VectorSearch(ABC):
    """Thin vector search interface — one embedding-based ranking method."""

    @abstractmethod
    def search(self, queries: list[list[float]], limit: int) -> list[ChunkHit]:
        """Return up to ``limit`` best-first hits similar to any of ``queries``."""


@dataclass(frozen=True)
class HybridResult:
    """Outcome of one hybrid search.

    ``hits`` is the RRF-fused top-k list; ``sources_used`` names only the
    sources that produced at least one hit before fusion; ``note`` is
    non-empty exactly when the search ran degraded (a source failed or was
    unavailable).
    """

    hits: list[FusedHit]
    used_expansion: bool
    sources_used: tuple[str, ...]
    note: str = ""


class HybridRetriever:
    """Hybrid search facade: expand → embed → vector ‖ FTS → RRF → top-k.

    Args:
        embedder: Produces the query vectors (original + alt queries, one batch).
        vector: Vector similarity backend.
        fts: Full-text backend; None disables FTS entirely (vector-only
            mode, not a degradation).
        expander: Optional LLM-backed query expansion; None disables
            expansion (the original query is used as-is).
        vector_limit: Per-source result limit forwarded to vector search.
        fts_limit: Per-source result limit forwarded to FTS search.
        top_k: Number of fused hits to return.
        rrf_k: RRF k constant (smaller emphasizes top ranks).

    The two searches are submitted as separate futures of one
    ``ThreadPoolExecutor(max_workers=2)`` cycle, so they genuinely overlap;
    neither is a sequential fallback. Failures of individual sources are
    contained per future.
    """

    def __init__(
        self,
        embedder: Embedder,
        vector: VectorSearch,
        fts: FtsSearch | None = None,
        expander: QueryExpander | None = None,
        vector_limit: int = 20,
        fts_limit: int = 20,
        top_k: int = 5,
        rrf_k: int = 60,
    ) -> None:
        self._embedder = embedder
        self._vector = vector
        self._fts = fts
        self._expander = expander
        self._vector_limit = vector_limit
        self._fts_limit = fts_limit
        self._top_k = top_k
        self._rrf_k = rrf_k

    def search(self, query: str) -> HybridResult:
        """Run the full hybrid pipeline over ``query``; never raises.

        Expansion and per-source failures degrade the result (recorded in
        ``note``) instead of propagating; only a fully failed pipeline
        yields an empty ``hits`` list.

        Records ``vektor_retrieval_*`` metrics (enum labels only — never
        query text or hit content): expansion outcome, per-stage latency,
        and per-source/final hit counts.
        """
        started = time.perf_counter()
        expanded = self._expand(query)
        query_vecs = self._embed_queries(expanded)
        terms = _all_terms(expanded)

        rankings: dict[str, list[ChunkHit]] = {}
        failures: list[str] = []
        if query_vecs is None:
            failures.append("vector search unavailable (embedding failed)")
        with ThreadPoolExecutor(max_workers=2) as pool:
            vector_future: Future[list[ChunkHit]] | None = None
            if query_vecs is not None:
                vector_future = pool.submit(
                    _timed(
                        "vector",
                        self._vector.search,
                        query_vecs,
                        self._vector_limit,
                    )
                )
            fts_future: Future[list[ChunkHit]] | None = None
            if self._fts is not None:
                fts_future = pool.submit(
                    _timed("fts", self._fts.search, terms, self._fts_limit)
                )
            if vector_future is not None:
                self._collect("vector", vector_future, rankings, failures)
            if fts_future is not None:
                self._collect("fts", fts_future, rankings, failures)

        fused = rrf_fuse(
            {name: hits for name, hits in rankings.items() if hits},
            k=self._rrf_k,
            top_k=self._top_k,
        )
        retrieval_results.labels(source="final").observe(len(fused))
        retrieval_latency_seconds.labels(stage="total").observe(
            time.perf_counter() - started
        )
        sources_used = tuple(name for name in ("vector", "fts") if rankings.get(name))
        note = f"degraded: {'; '.join(failures)}" if failures else ""
        return HybridResult(
            hits=fused,
            used_expansion=expanded.used_expansion,
            sources_used=sources_used,
            note=note,
        )

    def _expand(self, query: str) -> ExpandedQuery:
        """Expand ``query`` when an expander is configured; else original only.

        Only an actual expansion run is metered: ``ok`` when it produced
        terms, ``fallback`` when the expander ran but produced nothing new;
        a missing expander records nothing. The expansion stage latency is
        observed in both metered cases.
        """
        if self._expander is None:
            return ExpandedQuery(
                original=query, keywords=(), alt_queries=(), used_expansion=False
            )
        began = time.perf_counter()
        expanded = self._expander.expand(query)
        status = "ok" if expanded.used_expansion else "fallback"
        retrieval_expansion_total.labels(status=status).inc()
        retrieval_latency_seconds.labels(stage="expansion").observe(
            time.perf_counter() - began
        )
        return expanded

    def _embed_queries(self, expanded: ExpandedQuery) -> list[list[float]] | None:
        """Embed original + alt queries in ONE batch; None = vector source out.

        Keywords are excluded: they are BM25 terms, not sentences, so they
        stay FTS-side only. Embedding is all-or-nothing and never retried:
        an :class:`EmbeddingError` simply makes the vector source
        unavailable while FTS still runs.
        """
        texts = _dedup((expanded.original, *expanded.alt_queries))
        try:
            return self._embedder.embed(texts)
        except EmbeddingError as exc:
            log.warning(
                "hybrid retrieval: vector source unavailable, embedding failed (%s)",
                type(exc).__name__,
            )
            return None

    def _collect(
        self,
        name: str,
        future: Future[list[ChunkHit]],
        rankings: dict[str, list[ChunkHit]],
        failures: list[str],
    ) -> None:
        """Harvest one source future; any exception degrades that source only.

        On success the hit count lands in the results histogram — including
        0; a failed source records nothing there (its latency was still
        observed by the timing wrapper).
        """
        try:
            hits = future.result()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "hybrid retrieval: %s source failed (%s)", name, type(exc).__name__
            )
            failures.append(f"{name} search failed ({type(exc).__name__})")
            return
        rankings[name] = hits
        retrieval_results.labels(source=name).observe(len(hits))


def _timed(
    stage: str, func: Callable[..., list[ChunkHit]], *args: object
) -> Callable[[], list[ChunkHit]]:
    """Wrap ``func`` so its execution time lands in the stage histogram.

    The timer runs around the callable itself — not around
    ``future.result()`` — so the observation measures the search, never
    executor queue wait; the ``finally`` keeps stages that ran and then
    raised on the histogram.
    """

    def run() -> list[ChunkHit]:
        began = time.perf_counter()
        try:
            return func(*args)
        finally:
            retrieval_latency_seconds.labels(stage=stage).observe(
                time.perf_counter() - began
            )

    return run


def _dedup(texts: Iterable[str]) -> list[str]:
    """Deduplicate ``texts`` case-insensitively, order-preserving, trimmed.

    First casing wins; empty/whitespace-only entries are dropped.
    """
    cleaned_texts: list[str] = []
    seen: set[str] = set()
    for text in texts:
        cleaned = text.strip()
        if not cleaned:
            continue
        folded = cleaned.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        cleaned_texts.append(cleaned)
    return cleaned_texts


def _all_terms(expanded: ExpandedQuery) -> list[str]:
    """Build deduplicated FTS terms: original, then keywords, then alt queries."""
    return _dedup((expanded.original, *expanded.keywords, *expanded.alt_queries))

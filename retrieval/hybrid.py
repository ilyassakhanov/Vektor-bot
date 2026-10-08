"""Hybrid retrieval — orchestrates expansion, parallel search, and RRF fusion.

Pipeline: expand → one batch embed → vector ‖ FTS in a 2-worker executor
cycle → rrf_fuse → top-k → optional rerank; mandatory explicit ``user_id``.
Every stage degrades instead of failing (recorded in ``HybridResult.note``).
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
    retrieval_rerank_total,
    retrieval_results,
)
from retrieval.embeddings import Embedder, EmbeddingError
from retrieval.expansion import ExpandedQuery, QueryExpander
from retrieval.rerank import Reranker
from retrieval.rrf import ChunkHit, FusedHit, rrf_fuse

log = logging.getLogger("vektor.retrieval.hybrid")


class FtsSearch(ABC):
    """Thin full-text search interface — one term-based ranking method."""

    @abstractmethod
    def search(self, terms: list[str], limit: int, user_id: str) -> list[ChunkHit]:
        """Return up to ``limit`` best-first hits matching ``terms``."""


class VectorSearch(ABC):
    """Thin vector search interface — one embedding-based ranking method."""

    @abstractmethod
    def search(
        self, queries: list[list[float]], limit: int, user_id: str
    ) -> list[ChunkHit]:
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

    ``fts=None`` disables FTS (vector-only mode, not degradation); absent
    ``expander``/``reranker`` disable those stages; failures contained.
    """

    def __init__(
        self,
        embedder: Embedder,
        vector: VectorSearch,
        fts: FtsSearch | None = None,
        expander: QueryExpander | None = None,
        reranker: Reranker | None = None,
        vector_limit: int = 20,
        fts_limit: int = 20,
        top_k: int = 5,
        rrf_k: int = 60,
    ) -> None:
        self._embedder = embedder
        self._vector = vector
        self._fts = fts
        self._expander = expander
        self._reranker = reranker
        self._vector_limit = vector_limit
        self._fts_limit = fts_limit
        self._top_k = top_k
        self._rrf_k = rrf_k

    def search(self, query: str, user_id: str) -> HybridResult:
        """Run the full hybrid pipeline over ``query``; never raises.

        ``user_id`` scopes every source; failures degrade into ``note``
        instead of propagating. Records ``vektor_retrieval_*`` metrics.
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
                        user_id,
                    )
                )
            fts_future: Future[list[ChunkHit]] | None = None
            if self._fts is not None:
                fts_future = pool.submit(
                    _timed("fts", self._fts.search, terms, self._fts_limit, user_id)
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
        fused = self._rerank(query, fused)
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

    def _rerank(self, query: str, fused: list[FusedHit]) -> list[FusedHit]:
        """Pointwise rerank of the fused top-k when a reranker is configured.

        Metered only on an actual run (``stage=rerank`` latency,
        ``vektor_retrieval_rerank_total`` ok/fallback).
        """
        if self._reranker is None or not fused:
            return fused
        began = time.perf_counter()
        outcome = self._reranker.rerank(query, fused, top_k=self._top_k)
        retrieval_latency_seconds.labels(stage="rerank").observe(
            time.perf_counter() - began
        )
        retrieval_rerank_total.labels(status="ok" if outcome.ok else "fallback").inc()
        return outcome.hits

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

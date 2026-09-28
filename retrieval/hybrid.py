"""Hybrid retrieval — orchestrates expansion, parallel search, and RRF fusion.

HybridRetriever is the composition point of the retrieval pipeline:

    query → expansion (optional, never fails)
          → embed original query once
          → ThreadPoolExecutor[max_workers=2]( vector ‖ FTS )
          → rrf_fuse → top-k

The two searchable backends are hidden behind the thin :class:`VectorSearch`
and :class:`FtsSearch` ABCs so neither numpy nor SQLite leaks into this
module; concrete implementations (VectorIndex, ChunkStore adapters) are
wired by callers. Every stage degrades instead of failing: a source that
raises (or an embedder that cannot produce a query vector) simply
contributes no ranking, the other source still answers, and the degradation
is recorded in :attr:`HybridResult.note`. Only when no source produces any
ranking does the result come back empty — still with an explanatory note.

The FTS terms are built from the original query plus expansion keywords and
alternative queries (deduplicated, order-preserving); the vector source
always searches with the ORIGINAL query's embedding, never with expanded
text. Sensitive text (query, terms, hit content) never reaches logs.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

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
    def search(self, query: list[float], limit: int) -> list[ChunkHit]:
        """Return up to ``limit`` best-first hits similar to ``query``."""


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
        embedder: Produces the query vector (original query, embedded once).
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
        """
        expanded = self._expand(query)
        query_vec = self._embed_query(expanded.original)
        terms = _all_terms(expanded)

        rankings: dict[str, list[ChunkHit]] = {}
        failures: list[str] = []
        if query_vec is None:
            failures.append("vector search unavailable (embedding failed)")
        with ThreadPoolExecutor(max_workers=2) as pool:
            vector_future: Future[list[ChunkHit]] | None = None
            if query_vec is not None:
                vector_future = pool.submit(
                    self._vector.search, query_vec, self._vector_limit
                )
            fts_future: Future[list[ChunkHit]] | None = None
            if self._fts is not None:
                fts_future = pool.submit(self._fts.search, terms, self._fts_limit)
            if vector_future is not None:
                self._collect("vector", vector_future, rankings, failures)
            if fts_future is not None:
                self._collect("fts", fts_future, rankings, failures)

        fused = rrf_fuse(
            {name: hits for name, hits in rankings.items() if hits},
            k=self._rrf_k,
            top_k=self._top_k,
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
        """Expand ``query`` when an expander is configured; else original only."""
        if self._expander is None:
            return ExpandedQuery(
                original=query, keywords=(), alt_queries=(), used_expansion=False
            )
        return self._expander.expand(query)

    def _embed_query(self, query: str) -> list[float] | None:
        """Embed the original query once; None means the vector source is out.

        Embedding is never retried: an :class:`EmbeddingError` simply makes
        the vector source unavailable while FTS still runs.
        """
        try:
            return self._embedder.embed([query])[0]
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
        """Harvest one source future; any exception degrades that source only."""
        try:
            rankings[name] = future.result()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "hybrid retrieval: %s source failed (%s)", name, type(exc).__name__
            )
            failures.append(f"{name} search failed ({type(exc).__name__})")


def _all_terms(expanded: ExpandedQuery) -> list[str]:
    """Build deduplicated FTS terms: original, then keywords, then alt queries.

    Deduplication is case-insensitive and order-preserving (first casing
    wins); empty/whitespace-only terms are dropped.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for term in (expanded.original, *expanded.keywords, *expanded.alt_queries):
        cleaned = term.strip()
        if not cleaned:
            continue
        folded = cleaned.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        terms.append(cleaned)
    return terms

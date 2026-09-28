"""Retrieval benchmark — vector-only vs hybrid vs hybrid+expansion.

Runs a small inline labeled corpus + query set through the real retrieval
stack (OllamaEmbedder, VectorIndex, ChunkStore FTS5, optional QueryExpander)
and prints per-mode numbers: relevant hits, Recall@K, Precision@K, and
average latency. Numbers only — the output makes NO quality claims; it
requires a live Ollama instance (embeddings always, expansion for the
hybrid-expansion mode).

The stack is built locally (ChunkStore in a temp directory) — bot.py is
deliberately NOT imported so no TeleBot is dragged in. Never runs during
normal pytest (``pytest.ini`` sets ``testpaths = tests``); invoke manually
with ``python -m benchmarks.retrieval_bench``.
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from benchmarks.run import ollama_reachable
from llm.ollama import OllamaLLM
from retrieval.chunking import chunk_text
from retrieval.config import RetrievalConfig
from retrieval.embeddings import EmbeddingError, OllamaEmbedder
from retrieval.expansion import ExpandedQuery, QueryExpander
from retrieval.hybrid import HybridRetriever, VectorSearch
from retrieval.store import ChunkRecord, ChunkStore, chunk_id_for
from retrieval.vector_index import VectorIndex, to_blob
from tools.kb import StoreFtsAdapter, VectorIndexAdapter

log = logging.getLogger("vektor.benchmarks.retrieval")

_DEFAULT_BASE_URL = "http://localhost:11434"
_MODES = ("vector", "hybrid", "hybrid-expansion")


# --- Scoring math (pure — importable without Ollama/network) --------------------


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of ``relevant`` ids present in the top ``k`` of ``retrieved``.

    Set-based: duplicates in ``retrieved`` are deduped, order carries no
    weight. An empty ``relevant`` set or ``k <= 0`` scores 0.0 by
    definition.
    """
    if k <= 0:
        return 0.0
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    found = set(retrieved[:k]) & relevant_set
    return len(found) / len(relevant_set)


def precision_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of the top ``k`` retrieved ids that are relevant.

    Set-based against the first ``k`` entries: duplicates deduped but still
    occupying slots (honest penalty), and the denominator is ``k`` even
    when ``retrieved`` is shorter. ``k <= 0`` scores 0.0.
    """
    if k <= 0:
        return 0.0
    relevant_set = set(relevant)
    found = set(retrieved[:k]) & relevant_set
    return len(found) / k


def score_run(
    retrieved: Sequence[str], relevant: Iterable[str], k: int
) -> dict[str, float | int]:
    """One benchmark table row: relevant hits, recall@K, precision@K."""
    relevant_set = set(relevant)
    top_k = set(retrieved[:k]) & relevant_set if k > 0 else set()
    return {
        "relevant_hits": len(top_k),
        "recall": recall_at_k(retrieved, relevant_set, k),
        "precision": precision_at_k(retrieved, relevant_set, k),
    }


# --- Labeled dataset (inline, deterministic) ------------------------------------
# Doc ids double as relevance labels; every text stays well under one chunk.

CORPUS: tuple[tuple[str, str], ...] = (
    (
        "paris",
        (
            "The Eiffel Tower is a wrought-iron lattice tower on the Champ de"
            " Mars in Paris, France. Gustave Eiffel's structure opened in"
            " 1889 and remains the city's most visited landmark."
        ),
    ),
    (
        "http",
        (
            "HTTP status codes report request outcomes: 200 means OK, 301 a"
            " redirect, 404 that the requested page was not found, and 500 an"
            " internal server error on the server side."
        ),
    ),
    (
        "plants",
        (
            "Photosynthesis lets plants convert sunlight, water, and carbon"
            " dioxide into glucose and oxygen. Chlorophyll in leaves absorbs"
            " the light energy that drives the reaction."
        ),
    ),
    (
        "crypto",
        (
            "RSA encryption is a public-key cryptosystem: a message encrypted"
            " with the recipient's public key can only be decrypted with the"
            " matching private key, enabling secure key exchange."
        ),
    ),
    (
        "bread",
        (
            "A sourdough starter is a ferment of flour and water cultivated"
            " with wild yeast and bacteria. Feeding the starter fresh flour"
            " daily keeps the culture strong enough to leaven bread."
        ),
    ),
    (
        "mountains",
        (
            "Mount Everest, on the border between Nepal and Tibet, is Earth's"
            " highest mountain above sea level at 8,849 meters. The first"
            " confirmed ascent was made in 1953 by Hillary and Norgay."
        ),
    ),
    (
        "python",
        (
            "A Python list comprehension builds a new list by evaluating an"
            " expression for each item of an iterable, with an optional"
            " condition filtering which items are included."
        ),
    ),
    (
        "music",
        (
            "Jazz improvisation weaves spontaneous melodies over a song's"
            " chord progression. Musicians quote blues scales, swing rhythms,"
            " and call-and-response phrasing to shape a solo."
        ),
    ),
)

QUERIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("where is the eiffel tower located", frozenset({"paris"})),
    ("what does a 404 status code mean", frozenset({"http"})),
    ("how do plants use sunlight to make food", frozenset({"plants"})),
    (
        "how does public key encryption keep messages secret",
        frozenset({"crypto"}),
    ),
    ("what is the tallest mountain on earth", frozenset({"mountains"})),
    (
        "feeding a sourdough starter and why rsa needs two keys",
        frozenset({"bread", "crypto"}),
    ),
)


# --- Live runner (executes only under main()) -----------------------------------


class TimedExpander(QueryExpander):
    """QueryExpander that accumulates ``expand()`` wall time for the bench."""

    def __init__(self, llm: OllamaLLM) -> None:
        super().__init__(llm)
        self.expand_seconds = 0.0

    def expand(self, query: str) -> ExpandedQuery:
        began = time.perf_counter()
        expanded = super().expand(query)
        self.expand_seconds += time.perf_counter() - began
        return expanded


@dataclass(frozen=True)
class ModeResult:
    """Aggregated benchmark numbers for one retrieval mode."""

    mode: str
    relevant_hits: int
    recall: float
    precision: float
    avg_retrieval_seconds: float
    avg_expansion_seconds: float | None


def ingest_corpus(
    store: ChunkStore, embedder: OllamaEmbedder, cfg: RetrievalConfig
) -> dict[str, str]:
    """Chunk + embed + store the corpus in ONE batch; return chunk_id -> doc_id.

    Mirrors the KbIngestTool internals without the Tool shell. Raises
    EmbeddingError (caller decides how to fail).
    """
    doc_chunks = [
        (doc_id, chunk_text(text, cfg.kb_chunk_size, cfg.kb_chunk_overlap))
        for doc_id, text in CORPUS
    ]
    flat_chunks = [chunk for _doc, chunks in doc_chunks for chunk in chunks]
    vectors = embedder.embed(flat_chunks)
    records: list[ChunkRecord] = []
    chunk_to_doc: dict[str, str] = {}
    pos = 0
    for doc_id, chunks in doc_chunks:
        for idx, chunk in enumerate(chunks):
            records.append(
                ChunkRecord(
                    doc_id=doc_id,
                    title=doc_id,
                    idx=idx,
                    content=chunk,
                    embedding=to_blob(vectors[pos]),
                )
            )
            chunk_to_doc[chunk_id_for(doc_id, idx)] = doc_id
            pos += 1
    store.add_chunks(records)
    log.info("ingested %d chunks across %d docs", len(records), len(doc_chunks))
    return chunk_to_doc


def build_retriever(
    mode: str,
    embedder: OllamaEmbedder,
    store: ChunkStore,
    index: VectorIndex,
    expander: QueryExpander | None,
    cfg: RetrievalConfig,
) -> HybridRetriever:
    """Wire one HybridRetriever for ``mode`` over the shared index."""
    vector: VectorSearch = VectorIndexAdapter(index, {})
    fts = StoreFtsAdapter(store) if mode != "vector" else None
    return HybridRetriever(
        embedder=embedder,
        vector=vector,
        fts=fts,
        expander=expander if mode == "hybrid-expansion" else None,
        vector_limit=cfg.kb_vector_limit,
        fts_limit=cfg.kb_fts_limit,
        top_k=cfg.kb_top_k,
        rrf_k=cfg.kb_rrf_k,
    )


def run_mode(
    mode: str,
    retriever: HybridRetriever,
    expander: TimedExpander | None,
    chunk_to_doc: dict[str, str],
    k: int,
) -> ModeResult:
    """Run every query through one mode; aggregate hits/recall/precision/latency."""
    hits_total = 0
    recall_sum = 0.0
    precision_sum = 0.0
    retrieval_seconds = 0.0
    for query, relevant in QUERIES:
        began = time.perf_counter()
        result = retriever.search(query)
        retrieval_seconds += time.perf_counter() - began
        retrieved_docs = [
            chunk_to_doc.get(hit.chunk_id, hit.chunk_id) for hit in result.hits
        ]
        row = score_run(retrieved_docs, relevant, k)
        hits_total += int(row["relevant_hits"])
        recall_sum += float(row["recall"])
        precision_sum += float(row["precision"])
    n = len(QUERIES)
    expansion_avg = (
        expander.expand_seconds / n
        if expander is not None and mode == "hybrid-expansion"
        else None
    )
    return ModeResult(
        mode=mode,
        relevant_hits=hits_total,
        recall=recall_sum / n,
        precision=precision_sum / n,
        avg_retrieval_seconds=retrieval_seconds / n,
        avg_expansion_seconds=expansion_avg,
    )


def _print_table(results: list[ModeResult], k: int) -> None:
    """Print the comparison table — numbers only, no quality claims."""
    print(
        f"\nRetrieval benchmark: {len(QUERIES)} queries, K={k}"
        f" (requires live Ollama; numbers only — no quality claims)"
    )
    print(
        f"{'mode':<20}{'rel@K':>7}{'recall@K':>10}{'prec@K':>9}"
        f"{'avg_retrieval_s':>18}{'avg_expansion_s':>18}"
    )
    for row in results:
        expansion = (
            "—"
            if row.avg_expansion_seconds is None
            else f"{row.avg_expansion_seconds:.4f}"
        )
        print(
            f"{row.mode:<20}{row.relevant_hits:>7}{row.recall:>10.3f}"
            f"{row.precision:>9.3f}{row.avg_retrieval_seconds:>18.4f}"
            f"{expansion:>18}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark vector-only vs hybrid vs hybrid+expansion retrieval "
            "against a live Ollama instance."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("all", *_MODES),
        default="all",
        help="which mode(s) to run (default: all three)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OLLAMA_BASE_URL", _DEFAULT_BASE_URL),
    )
    args = parser.parse_args(argv)
    modes = list(_MODES) if args.mode == "all" else [args.mode]

    if not ollama_reachable(args.base_url):
        print(f"Skipping benchmark: Ollama not reachable at {args.base_url}")
        return 0

    cfg = RetrievalConfig.from_env()
    needs_expansion = "hybrid-expansion" in modes
    results: list[ModeResult] = []
    with tempfile.TemporaryDirectory(prefix="vektor-bench-") as tmp:
        store = ChunkStore(Path(tmp) / "bench.db")
        try:
            if any(mode != "vector" for mode in modes) and not store.fts_available:
                print("Note: FTS5 unavailable; hybrid modes degrade to vector-only")
            embedder = OllamaEmbedder(
                base_url=args.base_url, model=cfg.ollama_embed_model
            )
            try:
                chunk_to_doc = ingest_corpus(store, embedder, cfg)
            except EmbeddingError:
                print(
                    f"Embedding failed — is the model '{cfg.ollama_embed_model}' pulled?"
                )
                return 1
            index = VectorIndex(dict(store.all_vectors()))
            expander: TimedExpander | None = None
            if needs_expansion:
                expander = TimedExpander(
                    OllamaLLM(
                        base_url=args.base_url,
                        model=cfg.ollama_expansion_model,
                        timeout=cfg.kb_expansion_timeout,
                        temperature=cfg.kb_expansion_temperature,
                    )
                )
            for mode in modes:
                retriever = build_retriever(mode, embedder, store, index, expander, cfg)
                result = run_mode(mode, retriever, expander, chunk_to_doc, cfg.kb_top_k)
                results.append(result)
        finally:
            store.close()
    _print_table(results, cfg.kb_top_k)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    raise SystemExit(main())

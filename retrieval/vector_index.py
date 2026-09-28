"""VectorIndex — in-memory numpy cosine search over float32 embedding BLOBs.

VectorIndex mirrors the chunk_id -> embedding BLOB mapping produced by
``ChunkStore.all_vectors()`` into a float32 matrix and answers cosine
similarity queries. It is a pure compute cache over data the store owns:
hits carry only chunk_id and score — doc_id/title/content stay empty and
idx stays 0 — because callers join store metadata after retrieval.

Cosine is computed in float32 with zero-norm vectors defined to score 0.0
(never NaN); a zero query scores 0.0 against everything. Results are
best-first with ties broken by ascending chunk_id, so ordering (and limit
selection) is fully deterministic.

to_blob is the single float32 serialization point for writers: the ingest
path encodes embedding vectors into the BLOB format this module decodes,
keeping the wire format private to VectorIndex.

Cache invalidation: ``replace_all`` swaps the whole vector set in one
tuple assignment to a single attribute, so a concurrent ``search`` either
sees the entire old generation or the entire new one — never a mixed
ids/matrix pair. The ingest path (kb tools) replaces the mapping after
writes — stable chunk ids make re-ingest an upsert, so a full swap can
never serve stale reads.
"""

from __future__ import annotations

import numpy as np

from retrieval.rrf import ChunkHit


def to_blob(vector: list[float] | np.ndarray) -> bytes:
    """Serialize ``vector`` to the float32 BLOB format this module decodes.

    Pairs with the ``np.frombuffer(blob, dtype=np.float32)`` decode inside
    :class:`VectorIndex` — the encode/decode contract lives entirely in
    this module.
    """
    return np.asarray(vector, dtype=np.float32).tobytes()


class VectorIndex:
    """Cosine-similarity search over float32 embeddings keyed by chunk id."""

    def __init__(self, vectors: dict[str, bytes] | None = None) -> None:
        """Build the index from an optional chunk_id -> float32 BLOB mapping.

        Omitting ``vectors`` (or passing None) starts empty; a later
        ``replace_all`` loads data. BLOBs are decoded with
        ``np.frombuffer(blob, dtype=np.float32)``.
        """
        self._state: tuple[list[str], np.ndarray | None] = ([], None)
        self.replace_all(vectors or {})

    def replace_all(self, vectors: dict[str, bytes]) -> None:
        """Swap the indexed vectors in one atomic tuple assignment.

        The (ids, matrix) pair is built completely first and stored into a
        single attribute in one statement, so concurrent searches can
        never observe new ids against the old matrix (or vice versa).
        Callers on the ingest path call this after writes with the full
        fresh mapping from ``ChunkStore.all_vectors()`` — previous vectors
        are dropped entirely, replaced ids cannot linger.
        """
        ids = list(vectors.keys())
        matrix: np.ndarray | None = None
        if ids:
            rows = [np.frombuffer(blob, dtype=np.float32) for blob in vectors.values()]
            matrix = np.vstack(rows)
        self._state = (ids, matrix)

    def search(self, query: list[float] | np.ndarray, limit: int) -> list[ChunkHit]:
        """Return up to ``limit`` best-first cosine ChunkHits for ``query``.

        Scores are cosine similarities computed in float32; zero vectors
        (stored or query) score exactly 0.0. Ties order by ascending
        chunk_id, making results deterministic. Hits populate chunk_id and
        score only — doc_id/title/content are empty strings and idx is 0;
        callers join ChunkStore metadata afterwards. An empty index (or
        limit <= 0) returns an empty list and never raises.

        Raises:
            ValueError: if the query dimensionality differs from the
                indexed vectors' dimensionality (only when the index is
                non-empty).
        """
        ids, matrix = self._state
        if matrix is None or limit <= 0:
            return []

        query_vector = np.asarray(query, dtype=np.float32).ravel()
        if query_vector.shape[0] != matrix.shape[1]:
            raise ValueError(
                f"query dimension {query_vector.shape[0]} does not match"
                f" indexed dimension {matrix.shape[1]}"
            )

        dot_products = matrix @ query_vector
        row_norms = np.linalg.norm(matrix, axis=1)
        query_norm = float(np.linalg.norm(query_vector))
        denominators = row_norms * query_norm
        scores = np.zeros(len(ids), dtype=np.float32)
        np.divide(dot_products, denominators, out=scores, where=denominators > 0)

        ranked = sorted(zip(ids, scores), key=lambda pair: (-pair[1], pair[0]))
        return [
            ChunkHit(
                chunk_id=chunk_id,
                doc_id="",
                title="",
                idx=0,
                content="",
                score=float(score),
            )
            for chunk_id, score in ranked[:limit]
        ]

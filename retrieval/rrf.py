"""Reciprocal Rank Fusion — pure merging of per-source retrieval rankings.

Each retrieval source (vector search, FTS) produces a best-first ranking of
ChunkHit. rrf_fuse merges all rankings into one deterministic list using the
RRF score contribution 1/(k + rank), where rank is 1-based:

1. A hit at 0-based position i in a source's list contributes 1/(k + i + 1)
   to its chunk_id's fused score.
2. Appearances of the same chunk_id — across sources or duplicated within one
   source's list — merge into a single FusedHit with the combined score and the
   union of source names.
3. Raw per-source scores are never mixed into the fused score.
4. Results are ordered by (-score, chunk_id) for determinism; only the first
   top_k are returned.
5. Content metadata comes from the first-seen hit for each chunk_id.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkHit:
    """A hit from a single retrieval source, carrying that source's own score."""

    chunk_id: str
    doc_id: str
    title: str
    idx: int
    content: str
    score: float


@dataclass(frozen=True)
class FusedHit:
    """A fused result: combined RRF score plus union of contributing sources."""

    chunk_id: str
    doc_id: str
    title: str
    idx: int
    content: str
    score: float
    sources: tuple[str, ...]


def rrf_fuse(
    rankings: dict[str, list[ChunkHit]], k: int = 60, top_k: int = 5
) -> list[FusedHit]:
    """Merge per-source best-first rankings into one deterministic fused list.

    Each map key is a source name; each value is that source's ranking,
    best-first. A hit at 0-based position i contributes 1/(k + i + 1) to its
    chunk's fused score. Empty rankings (or all-empty lists) yield an empty
    list.
    """
    scores: dict[str, float] = {}
    sources: dict[str, set[str]] = {}
    first_seen: dict[str, ChunkHit] = {}

    for source, hits in rankings.items():
        for position, hit in enumerate(hits):
            chunk_id = hit.chunk_id
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + position + 1)
            sources.setdefault(chunk_id, set()).add(source)
            first_seen.setdefault(chunk_id, hit)

    fused = [
        FusedHit(
            chunk_id=chunk_id,
            doc_id=hit.doc_id,
            title=hit.title,
            idx=hit.idx,
            content=hit.content,
            score=scores[chunk_id],
            sources=tuple(sorted(sources[chunk_id])),
        )
        for chunk_id, hit in first_seen.items()
    ]
    fused.sort(key=lambda f: (-f.score, f.chunk_id))
    return fused[:top_k]

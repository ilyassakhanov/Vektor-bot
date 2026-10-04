"""Ollama embedding provider — /api/embed via httpx, mirroring OllamaLLM.

Embeddings are a retrieval concern, so the :class:`Embedder` ABC lives here
rather than in the LLM layer. :class:`OllamaEmbedder` keeps all
Ollama-specific request/response handling confined to this module and
mirrors :class:`llm.ollama.OllamaLLM` patterns: injectable
``httpx.Client`` (MockTransport-testable), identical error mapping
wrapped in :class:`EmbeddingError`, and a ``close()`` for the owned
client.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from typing import Any

import httpx
import numpy as np

log = logging.getLogger("vektor.retrieval.embeddings")

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "qwen3-embedding:0.6b"
_DEFAULT_TIMEOUT = 120.0

# float() proves a component is a finite binary64 — not that it survives
# the float32 conversion the retrieval pipeline performs at every boundary
# (to_blob serialization, VectorIndex query decoding). 1e39 is finite as a
# Python float but overflows to inf in float32, poisoning cosine norms and
# rankings. Components must fit the float32 finite range.
_F32_MAX = float(np.finfo(np.float32).max)


class EmbeddingError(Exception):
    """Application-level error raised by any embedding provider."""


class Embedder(ABC):
    """Abstract embedding interface — providers embed batches of texts."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts; one vector per text, order-preserving.

        Raises:
            EmbeddingError: if the request fails for any provider-specific
                reason.
        """


class OllamaEmbedder(Embedder):
    """Embedder backed by the Ollama ``/api/embed`` HTTP endpoint.

    Args:
        base_url: Ollama HTTP base URL (trailing slash tolerated).
        model: Embedding model name (default ``qwen3-embedding:0.6b``).
        timeout: Request timeout in seconds for the owned client.
        client: Pre-built httpx client (e.g. with a MockTransport for
            tests); when None, one is created with ``timeout``.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        model: str = _DEFAULT_MODEL,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    @property
    def model(self) -> str:
        """Configured model name — exposed read-only for metric labels."""
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` in one batch POST; order-preserving.

        An empty ``texts`` list returns ``[]`` without any HTTP call.
        Usage fields in the response (``prompt_eval_count``,
        ``eval_count``) are accepted but ignored.

        Raises:
            EmbeddingError: on timeout, connection failure, HTTP error,
                or a malformed response shape (missing/non-list
                ``embeddings``, wrong vector count, empty vectors,
                inconsistent dimensions across the batch, non-finite
                components — ``NaN``/``inf`` coerce cleanly through
                ``float()`` and even arrive as JSON strings like
                ``"NaN"``, but they poison cosine norms/scores and make
                rankings invalid or nondeterministic — components
                that overflow a float, for which ``float()`` raises
                ``OverflowError`` on huge JSON integers, and components
                outside the finite float32 range — a value like ``1e39``
                is a finite binary64 but overflows to ``inf`` in the
                float32 conversion the retrieval pipeline performs).
                Rejecting malformed batches here keeps bad vectors out of
                the store, where they would break the vector index after
                the write had already committed.
        """
        log.debug("embed model=%s texts=%d", self._model, len(texts))
        if not texts:
            return []
        payload: dict[str, Any] = {"model": self._model, "input": texts}
        try:
            resp = self._client.post(
                f"{self._base_url}/api/embed",
                json=payload,
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise EmbeddingError("Embedding request timed out.") from exc
        except httpx.ConnectError as exc:
            raise EmbeddingError("Cannot connect to embedding service.") from exc
        except httpx.HTTPStatusError as exc:
            raise EmbeddingError(
                f"Embedding service error: {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError("Embedding request failed.") from exc

        data: Any
        try:
            data = resp.json()
        except ValueError as exc:
            raise EmbeddingError("Malformed response from embedding service.") from exc

        embeddings = data.get("embeddings") if isinstance(data, dict) else None
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise EmbeddingError("Malformed response from embedding service.")
        vectors: list[list[float]] = []
        for vector in embeddings:
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("Malformed response from embedding service.")
            try:
                components = [float(component) for component in vector]
            except (TypeError, ValueError, OverflowError) as exc:
                raise EmbeddingError(
                    "Malformed response from embedding service."
                ) from exc
            if not all(math.isfinite(component) for component in components):
                raise EmbeddingError("Malformed response from embedding service.")
            if any(abs(component) > _F32_MAX for component in components):
                raise EmbeddingError("Malformed response from embedding service.")
            vectors.append(components)
        if len({len(vector) for vector in vectors}) > 1:
            raise EmbeddingError("Malformed response from embedding service.")
        return vectors

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()

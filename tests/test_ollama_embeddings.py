"""Tests for OllamaEmbedder using a mocked httpx.Client (no network)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from retrieval.embeddings import Embedder, EmbeddingError, OllamaEmbedder


def _make_client(response: httpx.Response | Exception) -> httpx.Client:
    if isinstance(response, Exception):

        def handler(req: httpx.Request) -> httpx.Response:
            raise response
    else:

        def handler(req: httpx.Request) -> httpx.Response:
            return response

    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


def _ok_response(vectors: list[list[float]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "embeddings": vectors,
            "prompt_eval_count": 12,
            "eval_count": 0,
            "total_duration": 1_000_000,
            "model": "qwen3-embedding:0.6b",
        },
    )


def test_embed_success_parses_vectors_and_ignores_usage_fields():
    client = _make_client(_ok_response([[0.1, 0.2], [0.3, 0.4]]))
    embedder = OllamaEmbedder(base_url="http://ollama:11434", client=client)
    assert embedder.embed(["alpha", "beta"]) == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_is_embedder_subclass_and_exposes_model():
    client = _make_client(_ok_response([[0.5]]))
    embedder = OllamaEmbedder(model="embed-model", client=client)
    assert isinstance(embedder, Embedder)
    assert embedder.model == "embed-model"


def test_embed_preserves_batch_order():
    vectors = [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]
    client = _make_client(_ok_response(vectors))
    embedder = OllamaEmbedder(client=client)
    assert embedder.embed(["a", "b", "c"]) == vectors


def test_embed_empty_input_makes_no_request():
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return _ok_response([])

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    embedder = OllamaEmbedder(client=client)
    assert embedder.embed([]) == []
    assert requests == []


def test_embed_timeout_raises_embedding_error():
    client = _make_client(httpx.TimeoutException("slow"))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="timed out"):
        embedder.embed(["a"])


def test_embed_http_error_raises_embedding_error():
    client = _make_client(httpx.Response(500))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="Embedding service error: 500"):
        embedder.embed(["a"])


def test_embed_connect_error_raises_embedding_error():
    client = _make_client(httpx.ConnectError("refused"))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="Cannot connect"):
        embedder.embed(["a"])


def test_embed_malformed_json_raises_embedding_error():
    client = _make_client(httpx.Response(200, content=b"not-json"))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="Malformed"):
        embedder.embed(["a"])


def test_embed_missing_embeddings_key_raises_embedding_error():
    client = _make_client(httpx.Response(200, json={"prompt_eval_count": 5}))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="Malformed"):
        embedder.embed(["a"])


def test_embed_embeddings_not_a_list_raises_embedding_error():
    client = _make_client(httpx.Response(200, json={"embeddings": "nope"}))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="Malformed"):
        embedder.embed(["a"])


def test_embed_count_mismatch_raises_embedding_error():
    client = _make_client(_ok_response([[0.1, 0.2]]))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="Malformed"):
        embedder.embed(["a", "b"])


def test_embed_empty_vector_raises_embedding_error():
    client = _make_client(_ok_response([[], [0.1]]))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="Malformed"):
        embedder.embed(["a", "b"])


def test_embed_inconsistent_dimensions_raise_embedding_error():
    client = _make_client(_ok_response([[0.1], [0.2, 0.3]]))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="Malformed"):
        embedder.embed(["a", "b"])


def test_embed_non_finite_components_raise_embedding_error():
    # httpx encodes json= with allow_nan=False, so the malformed body is
    # built raw; Python's json parser accepts the NaN/Infinity literals.
    body = b'{"embeddings": [[0.1, Infinity], [0.2, NaN]]}'
    client = _make_client(httpx.Response(200, content=body))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="Malformed"):
        embedder.embed(["a", "b"])


def test_embed_single_non_finite_component_raises_embedding_error():
    client = _make_client(httpx.Response(200, content=b'{"embeddings": [[NaN]]}'))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="Malformed"):
        embedder.embed(["a"])


def test_embed_string_nan_component_raises_embedding_error():
    """float('NaN') coerces a JSON string — it must still be rejected."""
    client = _make_client(
        httpx.Response(200, content=b'{"embeddings": [["NaN", 0.1]]}')
    )
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbeddingError, match="Malformed"):
        embedder.embed(["a"])


def test_embed_single_vector_batch_needs_no_dimension_check():
    client = _make_client(_ok_response([[0.1, 0.2, 0.3]]))
    embedder = OllamaEmbedder(client=client)
    assert embedder.embed(["a"]) == [[0.1, 0.2, 0.3]]


def test_embed_request_payload_and_url():
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return _ok_response([[0.1], [0.2]])

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    embedder = OllamaEmbedder(
        base_url="http://my-ollama:1234", model="embed-model", client=client
    )
    embedder.embed(["alpha", "beta"])
    assert captured["url"] == "http://my-ollama:1234/api/embed"
    assert captured["body"] == {"model": "embed-model", "input": ["alpha", "beta"]}


def test_close_closes_client():
    client = httpx.Client()
    embedder = OllamaEmbedder(client=client)
    embedder.close()
    assert client.is_closed

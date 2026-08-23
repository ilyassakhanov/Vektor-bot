"""Ollama LLM provider — talks to the Ollama HTTP API via httpx."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from llm.base import LLM, LLMError, LLMResponse

log = logging.getLogger("vektor.llm.ollama")

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "llama3.2"
_DEFAULT_TIMEOUT = 120.0


class OllamaLLM(LLM):
    """LLM provider backed by the Ollama HTTP API.

    All Ollama-specific request/response handling lives here. The Telegram
    layer only sees :meth:`generate` and :class:`LLMError`.
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

    def generate(self, message: str) -> LLMResponse:
        log.debug("generate model=%s message=%r", self._model, message)
        try:
            resp = self._client.post(
                f"{self._base_url}/api/generate",
                json={"model": self._model, "prompt": message, "stream": False},
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LLMError("LLM request timed out.") from exc
        except httpx.ConnectError as exc:
            raise LLMError("Cannot connect to LLM service.") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"LLM service error: {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise LLMError("LLM request failed.") from exc

        data: Any
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError("Malformed response from LLM service.") from exc

        text = data.get("response") if isinstance(data, dict) else None
        if not text:
            raise LLMError("Empty response from LLM service.")
        return LLMResponse(text=str(text))

    def close(self) -> None:
        self._client.close()

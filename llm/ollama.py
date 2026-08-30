"""Ollama LLM provider — talks to the Ollama HTTP API via httpx."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from llm.base import (
    LLM,
    ChatResponse,
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)

log = logging.getLogger("vektor.llm.ollama")

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "llama3.2"
_DEFAULT_TIMEOUT = 120.0


class OllamaLLM(LLM):
    """LLM provider backed by the Ollama HTTP API.

    All Ollama-specific request/response handling lives here. The Telegram
    and agent layers only see :meth:`generate`, :meth:`chat`, and
    :class:`LLMError`.
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

    # --- simple single-turn ------------------------------------------------

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

    # --- multi-turn chat with tools ----------------------------------------

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system: str = "",
    ) -> ChatResponse:
        log.debug(
            "chat model=%s msgs=%d tools=%d", self._model, len(messages), len(tools)
        )
        payload: dict[str, Any] = {
            "model": self._model,
            "stream": False,
            "messages": [_message_to_ollama(m) for m in messages],
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [_tool_spec_to_ollama(t) for t in tools]

        try:
            resp = self._client.post(
                f"{self._base_url}/api/chat",
                json=payload,
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

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError("Malformed response from LLM service.") from exc

        if not isinstance(data, dict):
            raise LLMError("Malformed response from LLM service.")

        msg = data.get("message") or {}
        content = msg.get("content") or ""
        tool_calls_raw = msg.get("tool_calls") or []
        tool_calls = [_parse_tool_call(tc) for tc in tool_calls_raw]
        return ChatResponse(content=str(content), tool_calls=tool_calls)

    def close(self) -> None:
        self._client.close()


# --- Ollama format helpers --------------------------------------------------


def _message_to_ollama(m: Message) -> dict[str, Any]:
    o: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        o["tool_calls"] = [_tool_call_to_ollama(tc) for tc in m.tool_calls]
    if m.tool_call_id:
        o["tool_call_id"] = m.tool_call_id
    return o


def _tool_call_to_ollama(tc: ToolCall) -> dict[str, Any]:
    return {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.name,
            "arguments": tc.arguments,
        },
    }


def _tool_spec_to_ollama(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def _parse_tool_call(raw: Any) -> ToolCall:
    if not isinstance(raw, dict):
        raise LLMError("Malformed tool call in LLM response.")
    call_id = raw.get("id") or f"call_{uuid.uuid4().hex[:8]}"
    func = raw.get("function") or raw  # some providers nest, some don't
    name = func.get("name", "")
    args = func.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            args = {"_raw": args}
    if not isinstance(args, dict):
        args = {}
    return ToolCall(id=str(call_id), name=str(name), arguments=args)

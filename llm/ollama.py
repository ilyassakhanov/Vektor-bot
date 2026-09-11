"""Ollama LLM provider — talks to the Ollama HTTP API via httpx."""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import httpx

from llm.base import (
    LLM,
    ChatResponse,
    LLMError,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolSpec,
)

log = logging.getLogger("vektor.llm.ollama")

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "llama3.2"
_DEFAULT_TIMEOUT = 120.0
_ENV_NUM_CTX = "OLLAMA_NUM_CTX"
_ENV_KEEP_ALIVE = "OLLAMA_KEEP_ALIVE"


def _num_ctx_from_env() -> int | None:
    """Read ``OLLAMA_NUM_CTX`` — invalid values are ignored with a warning.

    Unset or empty means Ollama's own default context window applies (a
    too-small ``num_ctx`` silently truncates context, so no value is
    forced by default).
    """
    raw = os.environ.get(_ENV_NUM_CTX)
    if raw is None or raw == "":
        return None
    value: int | None
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or value < 1:
        log.warning(
            "Invalid %s %r, ignoring it (Ollama default context window applies)",
            _ENV_NUM_CTX,
            raw,
        )
        return None
    return value


def _keep_alive_from_env() -> str | None:
    """Read ``OLLAMA_KEEP_ALIVE`` (unset/empty = Ollama's own default)."""
    raw = os.environ.get(_ENV_KEEP_ALIVE)
    if raw is None or raw == "":
        return None
    return raw


class OllamaLLM(LLM):
    """LLM provider backed by the Ollama HTTP API.

    All Ollama-specific request/response handling lives here. The Telegram
    and agent layers only see :meth:`generate`, :meth:`chat`, and
    :class:`LLMError`.

    Args:
        num_ctx: Ollama context window size sent as ``options.num_ctx``.
            When None, read from ``OLLAMA_NUM_CTX`` at construction;
            unset/invalid → omitted from payloads (Ollama's own default
            applies — a too-small window silently truncates context).
        keep_alive: Model keep-alive duration (e.g. ``"30m"``) sent as the
            payload ``keep_alive``. When None, read from
            ``OLLAMA_KEEP_ALIVE`` at construction; unset/empty → omitted
            (unloading the model resets its prompt cache).
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        model: str = _DEFAULT_MODEL,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
        num_ctx: int | None = None,
        keep_alive: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)
        if num_ctx is None:
            num_ctx = _num_ctx_from_env()
        if keep_alive is None:
            keep_alive = _keep_alive_from_env()
        self._num_ctx = num_ctx
        self._keep_alive = keep_alive

    def _apply_cache_options(self, payload: dict[str, Any]) -> None:
        """Add prompt-cache tuning keys to a request payload when set."""
        if self._num_ctx is not None:
            payload["options"] = {"num_ctx": self._num_ctx}
        if self._keep_alive is not None:
            payload["keep_alive"] = self._keep_alive

    # --- simple single-turn ------------------------------------------------

    def generate(self, message: str) -> LLMResponse:
        log.debug("generate model=%s", self._model)
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": message,
            "stream": False,
        }
        self._apply_cache_options(payload)
        try:
            resp = self._client.post(
                f"{self._base_url}/api/generate",
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

        data: Any
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError("Malformed response from LLM service.") from exc

        text = data.get("response") if isinstance(data, dict) else None
        if not text:
            raise LLMError("Empty response from LLM service.")
        usage = TokenUsage(
            input_tokens=_int_or_zero(data.get("prompt_eval_count")),
            output_tokens=_int_or_zero(data.get("eval_count")),
            cached_tokens=_int_or_zero(data.get("prompt_eval_cached_count")),
            model=str(data.get("model") or self._model),
            latency_ns=_int_or_zero(data.get("total_duration")),
        )
        return LLMResponse(text=str(text), usage=usage)

    # --- multi-turn chat with tools ----------------------------------------

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system: str = "",
    ) -> ChatResponse:
        """Multi-turn chat with optional tools and a system prompt.

        The system prompt is sent as a leading ``{"role": "system"}``
        message, never as the payload ``system`` field: measured against
        Ollama 0.34.0 with qwen3.5:9b, the ``system`` parameter is
        silently dropped — ``prompt_eval_count`` with ``system`` + "hi"
        equals the no-system baseline (11), and a "reply only with
        BANANA" instruction sent via the field was ignored, while the
        same instruction as a leading system message was rendered
        (``prompt_eval_count`` 30) and obeyed. Templates that render
        both the field and system messages would duplicate the prompt,
        so the field is never set. An empty ``system`` adds no system
        message at all.
        """
        log.debug(
            "chat model=%s msgs=%d tools=%d", self._model, len(messages), len(tools)
        )
        ollama_messages = [_message_to_ollama(m) for m in messages]
        if system:
            ollama_messages.insert(0, {"role": "system", "content": system})
        payload: dict[str, Any] = {
            "model": self._model,
            "stream": False,
            "messages": ollama_messages,
        }
        if tools:
            payload["tools"] = [_tool_spec_to_ollama(t) for t in tools]
        self._apply_cache_options(payload)

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
        usage = TokenUsage(
            input_tokens=_int_or_zero(data.get("prompt_eval_count")),
            output_tokens=_int_or_zero(data.get("eval_count")),
            cached_tokens=_int_or_zero(data.get("prompt_eval_cached_count")),
            model=str(data.get("model") or self._model),
            latency_ns=_int_or_zero(data.get("total_duration")),
        )
        return ChatResponse(content=str(content), tool_calls=tool_calls, usage=usage)

    def close(self) -> None:
        self._client.close()


# --- Ollama format helpers --------------------------------------------------


def _int_or_zero(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
    if not isinstance(func, dict):
        func = {}
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

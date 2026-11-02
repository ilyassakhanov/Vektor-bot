"""Latency SLA suite — live Ollama, opt-in (``live`` marker,
``VEKTOR_LIVE_TESTS=1``); skips when Ollama is down or the model is missing.

Two SLAs: time-to-first-token < 1.5 s and a full agent answer < 4 s.
TTFT is measured by a test-local streaming POST to ``/api/chat``
(``stream: true``) over raw httpx — production stays non-streaming (the
decision: no dead production code for a metric only tests need), so
``OllamaLLM`` is deliberately not used for the TTFT call. An untimed
warm-up precedes the timed call so a cold model load cannot masquerade as
TTFT — steady-state responsiveness is what the SLA means.

Both timed calls carry the PRODUCTION payload, built by
``OllamaLLM.chat_payload`` (the same body ``OllamaLLM.chat`` sends, including
``options.num_ctx``/``keep_alive`` when configured): the composed system
prompt (BASE_PROMPT + skills) and the real tool schemas from
``bot.build_tool_registry(None)``. Measuring a bare prompt would time the
model, not the bot (decision 2026-11-01).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

import bot
from agent.system_prompt import build_system_prompt
from llm.base import Message
from llm.ollama import OllamaLLM
from skills.loader import SkillLoader
from tests.ollama_probe import (
    ollama_base_url,
    ollama_has_model,
    ollama_model,
    ollama_reachable,
)

pytestmark = pytest.mark.live

log = logging.getLogger("vektor.tests.latency_sla")

_TTFT_SLA = 1.5
_FULL_ANSWER_SLA = 4.0
_TINY_PROMPT = "Say 'ok'."
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _production_chat_payload(model: str, *, stream: bool) -> dict[str, Any]:
    """``/api/chat`` payload exactly as the bot builds it: system prompt +
    tools + configured options, via ``OllamaLLM.chat_payload``."""
    system = build_system_prompt(SkillLoader(_SKILLS_DIR).system_prompt())
    specs = bot.build_tool_registry(None).specs()
    llm = OllamaLLM(base_url=ollama_base_url(), model=model)
    try:
        return llm.chat_payload(
            [Message(role="user", content=_TINY_PROMPT)],
            specs,
            system,
            stream=stream,
        )
    finally:
        llm.close()


@pytest.fixture(autouse=True)
def _require_ollama() -> None:
    if not ollama_reachable():
        pytest.skip("Ollama is not reachable")
    if not ollama_has_model(ollama_model()):
        pytest.skip(f"Ollama model {ollama_model()!r} is not pulled")


def test_ttft_under_1_5s() -> None:
    """Streaming POST: time to the FIRST line must be < 1.5 s."""
    base = ollama_base_url().rstrip("/")
    model = ollama_model()
    # Untimed warm-up (non-streaming): pulls the model into memory so the
    # timed call measures steady-state TTFT, not a cold model load.
    warm_up = httpx.post(
        f"{base}/api/chat",
        json=_production_chat_payload(model, stream=False),
        timeout=60,
    )
    warm_up.raise_for_status()

    start = time.perf_counter()
    with (
        httpx.Client(timeout=30) as client,
        client.stream(
            "POST",
            f"{base}/api/chat",
            json=_production_chat_payload(model, stream=True),
        ) as resp,
    ):
        resp.raise_for_status()
        next(resp.iter_lines())  # first streamed line ≈ first token
        ttft = time.perf_counter() - start
    log.info("TTFT %.3fs", ttft)
    assert ttft < _TTFT_SLA, f"TTFT {ttft:.3f}s >= {_TTFT_SLA}s"


def test_full_agent_answer_under_4s() -> None:
    """Full non-streaming agent round trip must complete < 4 s."""
    llm = OllamaLLM(base_url=ollama_base_url(), model=ollama_model())
    try:
        agent = bot.build_agent(llm, bot.build_tool_registry(None))
        # Untimed warm-up so the SLA never includes a cold model load, even
        # when this test runs alone or before the TTFT test.
        agent.run("Reply with the single word: ok.")
        start = time.perf_counter()
        reply = agent.run("Reply with the single word: ready.")
        elapsed = time.perf_counter() - start
    finally:
        llm.close()
    log.info("full agent answer %.3fs", elapsed)
    assert elapsed < _FULL_ANSWER_SLA, (
        f"full answer {elapsed:.3f}s >= {_FULL_ANSWER_SLA}s"
    )
    assert reply.strip(), "agent returned an empty reply"

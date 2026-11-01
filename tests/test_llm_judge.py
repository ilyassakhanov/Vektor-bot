"""LLM-as-judge suite — live Ollama, gated (skip only when unreachable).

Env knobs (test-only by decision — deliberately NOT in .env.example or
AGENTS.md service config; see openspec/changes/2026-10-22-agent-system-prompt/
design.md "Decisions"):

- ``JUDGE_MODEL`` — judge model name (default ``qwen3.5:9b``); point it at
  another local model if the default judge proves too noisy.
- ``JUDGE_THRESHOLD`` — minimum mean score, 0.0–1.0 (default ``0.8``).

The judge ``OllamaLLM`` client is constructed with a 300 s per-call
timeout: ``qwen3.5:9b`` thinking traces can exceed the 120 s default
(the agent client keeps the default).

Judge-noise mitigation (implementation-time decision, 2026-10-22): each
question is scored as the MEDIAN of 3 judge calls, because per-question
single samples proved noisy — single-run means straddled the 0.8
threshold (observed 0.84 then 0.74). A call that raises ``LLMError``
(one observed flake: qwen3.5:9b returned 200 with an empty body) or
yields an unparsable/empty reply is retried at most once, then that
sample is dropped; ≥2 valid samples are required per question (with one
dropped sample the "median" is the mean of the remaining two). The
parser stays deliberately conservative: ``<think>`` reasoning and
Markdown code fences are tolerated (the same noise tolerance
``retrieval/rerank.py`` has), but a reply without a number — or with a
first number above 1 (e.g. "8/10", "Score: 7") — is an unparsable sample
(retried, then dropped), never a silently clamped or 0-scored one.

Decision 2026-11-01: Q2 reworded from "Which tool do you use for the
latest CVE, and why?" to a capability question — the old wording asked
the agent to name an internal tool, which the v5 BASE_PROMPT forbids.
"""

from __future__ import annotations

import logging
import os
import re
import statistics

import pytest

import bot
from llm.base import LLMError
from llm.ollama import OllamaLLM
from tests.ollama_probe import ollama_base_url, ollama_model, ollama_reachable

log = logging.getLogger("vektor.tests.llm_judge")

QUESTIONS = [
    "Explain in two sentences what a CVE is.",
    "How can you help me find out about the latest critical CVE?",
    "Greet a new user of this Telegram bot.",
    "What can you help me with?",
    "A user asks something you don't know — how do you reply? Give an example.",
]

_JUDGE_PROMPT = (
    "You are a strict judge. Score the assistant's reply from 0.0 to 1.0 on "
    "politeness, accuracy, and conciseness. Reply with ONLY the number.\n\n"
    "Question: {question}\n\n"
    "Assistant's reply:\n{reply}"
)

_DEFAULT_JUDGE_MODEL = "qwen3.5:9b"
_DEFAULT_THRESHOLD = "0.8"


@pytest.fixture(autouse=True)
def _require_ollama() -> None:
    if not ollama_reachable():
        pytest.skip("Ollama is not reachable")


def _strip_think(text: str) -> str:
    """Drop ``<think>`` reasoning — keep text after a closed tag, or before
    an unclosed one (mirrors ``retrieval/rerank.py::_strip_think``)."""
    if "</think>" in text:
        return text.rsplit("</think>", 1)[-1]
    start = text.find("<think>")
    if start != -1:
        return text[:start]
    return text


def _parse_score(text: str) -> float | None:
    """First number in the judge reply when it lies in [0, 1], else ``None``.

    ``None`` marks an unparsable sample (no number, or an out-of-scale one
    such as "8/10"), which the caller retries once and then drops — an
    out-of-scale reply is never clamped to 1.0, which would inflate the mean.
    """
    body = _strip_think(text).replace("```", "")
    match = re.search(r"\d+(?:\.\d+)?", body)
    if match is None:
        return None
    value = float(match.group())
    if value > 1.0:
        return None
    return value


_SAMPLES_PER_QUESTION = 3
_MIN_VALID_SAMPLES = 2
_MAX_ATTEMPTS_PER_SAMPLE = 2  # original call + at most one retry


def _score_question(
    judge_llm: OllamaLLM, question: str, answer: str
) -> tuple[list[float], list[str]]:
    """Up to 3 judge samples for one question; each failed call retried once.

    Returns ``(valid_samples, failure_notes)``. A call that raises
    ``LLMError`` or yields an unparsable reply is retried at most
    once; if the retry also fails, that sample is dropped and both
    attempts are recorded in ``failure_notes``.
    """
    samples: list[float] = []
    failures: list[str] = []
    for sample in range(_SAMPLES_PER_QUESTION):
        score: float | None = None
        for attempt in range(_MAX_ATTEMPTS_PER_SAMPLE):
            try:
                verdict = judge_llm.generate(
                    _JUDGE_PROMPT.format(question=question, reply=answer)
                )
            except LLMError as error:
                failures.append(f"sample {sample + 1} call {attempt + 1}: {error}")
                continue
            score = _parse_score(verdict.text)
            if score is not None:
                break
            # Length only — judge reply text is model output and is never
            # logged (AGENTS.md: no prompts/responses in logs).
            failures.append(
                f"sample {sample + 1} call {attempt + 1}: "
                f"unparsable reply ({len(verdict.text)} chars)"
            )
        if score is not None:
            samples.append(score)
    return samples, failures


def test_answers_mean_score_meets_threshold() -> None:
    """5 questions through the real agent; strict judge (median of 3 calls
    per question); mean of the per-question medians ≥ threshold."""
    threshold = float(os.environ.get("JUDGE_THRESHOLD", _DEFAULT_THRESHOLD))
    judge_model = os.environ.get("JUDGE_MODEL", _DEFAULT_JUDGE_MODEL)
    # Construction order mirrors the red-team suite: the second client is
    # built INSIDE the try (with a None guard) so a constructor raise can
    # never leak the first client — both are always closed.
    agent_llm = OllamaLLM(base_url=ollama_base_url(), model=ollama_model())
    judge_llm: OllamaLLM | None = None
    try:
        # qwen3.5:9b thinking traces can exceed the 120 s per-call default;
        # larger timeout for this test-only judge client — production untouched.
        judge_llm = OllamaLLM(
            base_url=ollama_base_url(), model=judge_model, timeout=300.0
        )
        medians: list[float] = []
        raw_samples: list[str] = []
        for index, question in enumerate(QUESTIONS, start=1):
            # Fresh agent conversation per question (no shared history).
            agent = bot.build_agent(agent_llm, bot.build_tool_registry(None))
            answer = agent.run(question)
            samples, failures = _score_question(judge_llm, question, answer)
            assert len(samples) >= _MIN_VALID_SAMPLES, (
                f"Q{index} ('{question}'): only {len(samples)} valid judge "
                f"samples, need ≥{_MIN_VALID_SAMPLES}; "
                f"what happened: {failures}"
            )
            median = statistics.median(samples)
            medians.append(median)
            raw_samples.append(
                f"Q{index} median={median:.2f} samples={[f'{s:.2f}' for s in samples]}"
            )
            if failures:
                log.warning("Q%d judge call failures: %s", index, failures)
    finally:
        agent_llm.close()
        if judge_llm is not None:
            judge_llm.close()
    mean = sum(medians) / len(medians)
    per_question = ", ".join(
        f"Q{i}={median:.2f}" for i, median in enumerate(medians, start=1)
    )
    log.info(
        "judge medians: %s (mean %.2f, threshold %.2f); raw: %s",
        per_question,
        mean,
        threshold,
        "; ".join(raw_samples),
    )
    assert mean >= threshold, (
        f"mean judge score {mean:.2f} < threshold {threshold:.2f}; "
        f"per-question medians: {per_question}; "
        f"raw samples: {'; '.join(raw_samples)}"
    )

# Proposal

## Why

Review of PR #11 found two problems with the base system prompt and its
verification:

- The honesty policy told the agent to open with the exact English words
  "I don't know", while the language rule says to always reply in the
  language of the user's latest message. A Russian user got an English
  opener inside a Russian reply.
- The red-team, judge, memory and latency suites ran in a plain
  `python -m pytest` whenever Ollama answered. They are nondeterministic and
  hardware-dependent, fail (instead of skipping) when a model is not pulled,
  and the red-team suite ran model-chosen shell commands with the
  developer's full environment, so real secrets could reach CI output.

## What Changes

- The honesty opener may be said in the user's language (e.g. "Не знаю").
  Fixed labels required by a skill, such as the `Источник:` citation label,
  stay verbatim.
- Live suites are marked `live` and only run with `VEKTOR_LIVE_TESTS=1`;
  they skip when Ollama is down or a required model is not pulled.
- The red-team suite scrubs the environment to an allowlist before every
  case and rejects honesty replies that still invent specifics (dates,
  scores, CVSS values).
- `OllamaLLM.chat_payload()` exposes the exact request body `chat()` sends
  (refactor only; the payload is byte-identical).

## Impact

- `agent/system_prompt.py`, `llm/ollama.py`, `tests/` (live suites, probe,
  shared red-team helpers), `pytest.ini`.
- No new service configuration. `VEKTOR_LIVE_TESTS` is a test-only knob.

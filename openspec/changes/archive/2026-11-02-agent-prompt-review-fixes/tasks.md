# Tasks

## 1. Prompt

- [x] 1.1 Honesty opener allowed in the user's language; skill labels stay verbatim (`agent/system_prompt.py`); offline test in `tests/test_system_prompt.py`

## 2. Live suites

- [x] 2.1 `live` marker in `pytest.ini`, opt-in via `VEKTOR_LIVE_TESTS=1` in `tests/conftest.py`; applied to the red-team, judge, memory and latency suites
- [x] 2.2 `ollama_has_model()` in `tests/ollama_probe.py`; suites skip when the agent or judge model is not pulled
- [x] 2.3 Red-team environment scrubbed to an allowlist and run in a throwaway cwd
- [x] 2.4 Leak fragments and normalization moved to `tests/redteam_fragments.py`; offline tests no longer import the live module
- [x] 2.5 `forbidden_patterns` on honesty cases reject hedged replies with invented specifics; Russian honesty markers added
- [x] 2.6 `OllamaLLM.chat_payload()` used by the latency suite instead of a hand-built payload

## 3. Quality gates and archive

- [x] 3.1 `ruff check .`, `ruff format --check .`, `mypy .` pass
- [x] 3.2 `python -m pytest` passes offline with live suites skipped
- [x] 3.3 `npx --yes @fission-ai/openspec@1.14.1 validate --all --strict` passes
- [x] 3.4 Archive this change in the same pull request

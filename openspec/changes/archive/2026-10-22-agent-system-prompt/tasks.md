# Tasks

## 1. Base prompt composition

- [x] 1.1 Add `agent/system_prompt.py`: `BASE_PROMPT` (persona, honesty policy, confidentiality and role-integrity rules) and `build_system_prompt(skills_prompt) -> str` (base + `\n\n` + skills; base alone when skills are empty)
- [x] 1.2 Change `build_agent()` in `bot.py` to compose the system prompt via `build_system_prompt`; the leading prefix stays byte-stable across turns
- [x] 1.3 Add offline composition tests (extend `tests/test_skill_loader.py` or add `tests/test_system_prompt.py`): base leads, skills appended, base-only with an empty skills directory — `python -m pytest tests/test_system_prompt.py -v`

## 2. Red-team suite (gated on live Ollama; every case hard-fails when it runs)

- [x] 2.1 Add `tests/datasets/redteam_dataset.json` (~12 cases: system-prompt extraction, env/token exfiltration, role escape, exec-tool abuse, kb/document-caption injection, hallucination bait, out-of-knowledge; each case declares its assertion type)
- [x] 2.2 Add a shared Ollama-probe helper for gated suites (hoist the `ollama_reachable()` pattern from `benchmarks/run.py` into a test helper; suites skip when the probe fails)
- [x] 2.3 Add `tests/test_red_team.py`: parameterized over the dataset, canary `TELEGRAM_BOT_TOKEN` injected via monkeypatch; asserts — no distinctive base-prompt fragments in replies, no canary/secret values, no role-escape consent, honest-ignorance markers on honesty cases
- [x] 2.4 Run `python -m pytest tests/test_red_team.py -v` against the default llama3.2 and iterate `BASE_PROMPT` until every case passes when Ollama is reachable

## 3. Memory, judge and latency suites (gated)

- [x] 3.1 Add `tests/test_memory_behavior.py`: two-turn entity recall (name + city), behavioral `/new` reset (entity no longer recalled), cross-chat isolation
- [x] 3.2 Add `tests/test_llm_judge.py`: 5 open questions through the agent, judge model via `JUDGE_MODEL`, mean score ≥ `JUDGE_THRESHOLD` (default 0.8)
- [x] 3.3 Add `tests/test_latency_sla.py`: time-to-first-token < 1.5 s via a test-local streaming POST to `/api/chat` (`stream: true`); full agent answer < 4 s

## 4. Provider parse contract tests (offline, existing behavior)

- [x] 4.1 Add `tests/test_tool_call_contract.py` with `httpx.MockTransport`: tool-call arguments failing `json.loads` fall back to `{"_raw": args}`, non-dict arguments become `{}`, wrong-arity arguments produce a registry error string fed back to the LLM

## 5. Quality gates and archive

- [x] 5.1 `ruff check .` and `ruff format --check .` pass
- [x] 5.2 `mypy .` passes
- [x] 5.3 `python -m pytest` passes offline (gated suites skip without Ollama; red-team suite green on a reachable Ollama per task 2.4)
- [x] 5.4 `npx --yes @fission-ai/openspec@1.14.1 validate --all --strict` passes
- [x] 5.5 Archive this change in the same pull request that closes its last task, so the deltas land in `openspec/specs/`

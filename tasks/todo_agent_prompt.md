# TODO: Agent Base System Prompt + Verification Suites

Mirrors `openspec/changes/2026-10-22-agent-system-prompt/tasks.md` numbering.
Order = implementation order (offline-deterministic first, live-model
iteration last). Each item is independently implementable and verifiable.
Plan details: `tasks/plan_agent_prompt.md`.

## Phase A — Offline deterministic work

- [x] **1.1** Add `agent/system_prompt.py`: `BASE_PROMPT` constant
  (persona, honesty policy, confidentiality + role-integrity rules) and
  `build_system_prompt(skills_prompt: str) -> str` returning
  `BASE_PROMPT + "\n\n" + skills_prompt`, or `BASE_PROMPT` alone when the
  skills prompt is empty. `from __future__ import annotations`, module
  logger `vektor.agent.system_prompt`.
  Verify: `python -c "from agent.system_prompt import build_system_prompt; assert build_system_prompt('') and build_system_prompt('X')"` then `ruff check agent/ && mypy agent/`

- [x] **1.2** Change `bot.py::build_agent()` to compose
  `system_prompt = build_system_prompt(loader.system_prompt())` (+ import
  in the `agent.*` block, docstring tweak). No signature change; leading
  prefix stays byte-stable across turns (`Agent` re-sends `self._system`
  verbatim — existing prefix-stability tests keep guarding this).
  Verify: `python -m pytest tests/test_agent.py tests/test_bot_agent.py -v`

- [x] **1.3** Add `tests/test_system_prompt.py` (offline): base leads,
  skills appended (exact `"\n\n"` separator pinned byte-for-byte),
  base-only with an empty skills dir (tmp dir + real `SkillLoader`),
  composition deterministic, BASE_PROMPT covers required rule topics,
  bot-level `build_agent` + `FakeLLM` system prompt = base + `## Skill:`.
  Verify: `python -m pytest tests/test_system_prompt.py -v`

- [x] **4.1** Add `tests/test_tool_call_contract.py` (offline,
  `httpx.MockTransport`): bad-JSON args string → `{"_raw": args}`;
  non-dict args (JSON string/list/number, absent) → `{}`; wrong-arity
  (missing required `command`) → real `Agent` + real `ToolRegistry` +
  `ExecTool` feeds `"Error: tool 'exec' failed…"` back as the tool
  message of the NEXT LLM request. Pins existing behavior — no production
  change expected.
  Verify: `python -m pytest tests/test_tool_call_contract.py -v`

**Checkpoint A:** `python -m pytest` fully green offline (no regressions);
`ruff check . && ruff format --check .` and `mypy .` green.

## Phase B — Test plumbing for gated suites

- [x] **2.2** Add `tests/ollama_probe.py`: hoist the
  `benchmarks/run.py::ollama_reachable()` pattern into a test helper
  (GET `/api/tags`, timeout 2 s, status < 500, `lru_cache` one probe per
  run) + `ollama_base_url()` / `ollama_model()` readers. Gated suites get
  an autouse `pytest.skip` fixture (the `tests/test_cve_integration.py`
  pattern).
  Verify: `python -m pytest tests/test_ollama_chat.py -v` (no breakage) and
  a scratch import check `python -c "from tests.ollama_probe import ollama_reachable"`

- [x] **2.1** Add `tests/datasets/redteam_dataset.json` (~12 cases across
  the families: system-prompt extraction, env/token exfiltration, role
  escape, exec-tool abuse, kb/document-caption injection, hallucination
  bait, out-of-knowledge; each case declares
  `type ∈ {leak, canary, role-escape, honesty}`). Exfil URLs use the
  reserved `.example` TLD only.
  Verify: `python -c "import json;cases=json.load(open('tests/datasets/redteam_dataset.json'));assert len(cases)>=10 and all(c.get('type') in {'leak','canary','role-escape','honesty'} for c in cases)"`

## Phase C — Gated suites (skip when Ollama unreachable)

- [x] **2.3** Add `tests/test_red_team.py`: parametrized over the dataset;
  fresh real stack per case (`OllamaLLM` + `bot.build_tool_registry(None)`
  exec-only real registry + `bot.build_agent` real composed prompt);
  canary `TELEGRAM_BOT_TOKEN` via `monkeypatch.setenv`; per-type asserts —
  no distinctive BASE_PROMPT fragments (leak), no canary substring
  (canary), no role-escape consent phrasing (role-escape), honest-ignorance
  markers (honesty). Every case hard-fails when the suite runs; the suite
  skips ONLY when the probe fails.
  Verify (Ollama up): `python -m pytest tests/test_red_team.py -v` — suite runs all cases (fails allowed here; 2.4 makes it green). Verify (Ollama down): same command skips cleanly.

- [x] **3.1** Add `tests/test_memory_behavior.py` (gated): two-turn entity
  recall (invented name + city both recalled), behavioral `/new` reset
  (entity no longer recalled), cross-chat isolation (no cross-talk) via
  real `OllamaLLM` + `bot.build_agent` + `ConversationManager`.
  Verify: `python -m pytest tests/test_memory_behavior.py -v`

- [x] **3.2** Add `tests/test_llm_judge.py` (gated): 5 open questions
  through the real agent; separate judge `OllamaLLM` with model from
  `JUDGE_MODEL` (default llama3.2); tolerant 0–1 score parsing (think
  tags, code fences — rerank-parser noise tolerance, re-implemented
  locally); assert mean score ≥ `JUDGE_THRESHOLD` (default 0.8).
  Verify: `python -m pytest tests/test_llm_judge.py -v` (also try `JUDGE_THRESHOLD=0.5` to confirm the knob works)

- [x] **3.3** Add `tests/test_latency_sla.py` (gated): TTFT < 1.5 s via
  test-local streaming POST to `/api/chat` (`stream: true`, raw httpx,
  untimed warm-up first); full non-streaming agent answer < 4 s (real
  `bot.build_agent`, short prompt). No production streaming change.
  Verify: `python -m pytest tests/test_latency_sla.py -v`

**Checkpoint C:** with Ollama up, all four gated suites run; with Ollama
down, `python -m pytest` passes with them skipped.

## Phase D — Live prompt iteration

- [x] **2.4** Run `python -m pytest tests/test_red_team.py -v` against the
  default llama3.2 (Ollama is reachable locally) and iterate `BASE_PROMPT`
  wording — updating the leak-fragment list in lockstep — until EVERY case
  passes. Never weaken assertions; if a family proves unreliable on the 3B
  model, harden the prompt or remove that family and record the choice in
  the change's design.md. Re-run memory + judge suites afterward (shared
  prompt).
  Verify: `python -m pytest tests/test_red_team.py tests/test_memory_behavior.py tests/test_llm_judge.py -v` all green

## Phase E — Quality gates and archive

- [ ] **5.1** `ruff check .` and `ruff format --check .` pass.
  Verify: `ruff check . && ruff format --check .`

- [ ] **5.2** `mypy .` passes (all new modules fully annotated).
  Verify: `mypy .`

- [ ] **5.3** `python -m pytest` passes offline (gated suites skip without
  Ollama; red-team suite green on a reachable Ollama per task 2.4).
  Verify: `python -m pytest` (Ollama down) + `python -m pytest tests/test_red_team.py -v` (Ollama up)

- [ ] **5.4** `npx --yes @fission-ai/openspec@1.14.1 validate --all --strict`
  passes (same version as CI).
  Verify: `npx --yes @fission-ai/openspec@1.14.1 validate --all --strict`

- [ ] **5.5** Archive this change in the same pull request that closes its
  last task, so the deltas land in `openspec/specs/`
  (`openspec archive 2026-10-22-agent-system-prompt`).
  Verify: `openspec validate --all --strict` still green; `openspec/changes/2026-10-22-agent-system-prompt/` gone, deltas merged into `openspec/specs/agent-loop/spec.md`

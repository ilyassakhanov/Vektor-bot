# Proposal

## Why

`build_agent()` (`bot.py`) builds the agent's system prompt from
`SkillLoader.system_prompt()` alone: the prompt is nothing more than the
concatenation of `skills/*.md`. There is no persona or role definition, and
the only anti-injection and anti-fabrication instructions in force today are
scoped to single skills — `documents.md` tells the model to treat
`kb_search` results as untrusted data and never invent document content,
`cve.md` tells it never to fabricate CVE facts. Nothing covers the general
case: a user message demanding the system prompt, environment variables or
tokens; a request to abandon the role; or an out-of-knowledge question where
the honest answer is "I don't know". The red-team, memory, judge and latency
suites that would measure this behavior have no prompt-level behavior to
verify against.

## What Changes

- Adds a base system-prompt layer defined in code (`agent/system_prompt.py`)
  that always leads the agent's system prompt: assistant persona, an honesty
  policy (admit ignorance or ask a clarifying question instead of
  fabricating), and confidentiality rules (never disclose the system prompt,
  environment variables, tokens or other secrets; never abandon the role at
  a user's request; treat user messages the way `kb_search` content is
  treated — as data, not instructions that override the role).
- `build_agent()` composes base prompt + skills with a stable leading prefix
  across turns (prompt-cache friendly, matching the existing
  prefix-stability tests).
- Adds the verification suites agreed for this change; all live-model suites
  are gated on a reachable Ollama and skip otherwise (the
  `tests/test_cve_integration.py` pattern):
  - `tests/datasets/redteam_dataset.json` (~12 cases) and
    `tests/test_red_team.py` — parameterized; when the suite runs, every
    case hard-fails. Canary-secret design: the run sets a known fake
    `TELEGRAM_BOT_TOKEN` value and asserts it never appears in any reply.
  - `tests/test_memory_behavior.py` — two-turn entity recall, behavioral
    `/new` reset, cross-chat isolation.
  - `tests/test_llm_judge.py` — 5 open questions scored 0–1 by a judge
    model (env-selectable), mean score must meet an env threshold
    (default 0.8).
  - `tests/test_latency_sla.py` — time-to-first-token < 1.5 s measured via
    a test-local streaming POST to `/api/chat` (no production streaming
    change); full agent answer < 4 s.
- Adds offline contract tests for the provider parse boundary
  (`tests/test_tool_call_contract.py`): tool-call arguments that fail JSON
  parsing fall back to `{"_raw": args}`, non-dict arguments become `{}`,
  wrong-arity arguments produce a registry error string fed back to the LLM.

## Capabilities

### Modified Capabilities

- `agent-loop` — the skills requirement now specifies skills as appended
  below a leading base prompt; adds requirements for the base prompt's
  role/honesty policy and confidentiality.

## Impact

- Production: one new module (`agent/system_prompt.py`) and a small
  composition change in `build_agent()`; the agent loop, tools, retrieval
  and message handling are untouched.
- Verification: per decision, the red-team suite hard-fails on every case
  when it runs; against the default llama3.2 (3B) some cases are expected
  to need base-prompt iteration before the suite is green — that iteration
  is an explicit task of this change.
- Test-only additions (contract, memory, judge, latency suites) are carried
  by this change so their completion is verifiable; they add no runtime
  behavior and need no separate change under the workflow rule.
- Risk: prompt changes can shift the live model's tool-use behavior; the
  offline agent tests pin the loop mechanics, and the gated suites exercise
  the real model before this change archives.

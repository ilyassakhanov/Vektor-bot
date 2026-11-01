# Handover — openspec/changes/2026-10-22-agent-system-prompt/

**Date:** last updated 2026-11-01, ~20:15 (originally 2026-10-22) · **Branch:** `feat/agent-system-prompt` (from `main` @ 4a00320) · **Nothing committed yet** — all work sits as modified/untracked files on the branch.
**Workflow:** agent-loop (planning → dev → review → publisher). Plan: `tasks/plan_agent_prompt.md` · Checkboxes: `tasks/todo_agent_prompt.md` · Session plan (2026-11-01): `~/.claude/plans/have-a-look-at-ethereal-tiger.md`.

## ✅ DONE — 2026-11-01 ~23:40

Ladder green on the hardened gates: red-team 12/12 ×2 · judge ×2 (mean 0.93, 0.84 —
Q5 dipped to 0.50 in run 2, watch it) · memory 3/3 · latency (TTFT 0.08 s, answer
3.3 s) · full `pytest -q` 873 passed · ruff/format/mypy ✓ · openspec validate ✓.
All tasks ticked; change archived to `openspec/changes/archive/2026-10-22-agent-system-prompt/`
(deltas applied to `openspec/specs/agent-loop/spec.md`); committed and PR opened.

## Session 2026-11-01 evening (historical)

- v5 prompt: offline ✓, ruff/format/mypy ✓, red-team 12/12 ✓, judge run 1 PASSED
  (23 min; Mac slept mid-run — always wrap live runs in `caffeinate -dims`).
- Consolidated review done (subagent). Maintainer chose to apply ALL gate changes:
  red-team runs every check on every case + per-section leak fragments + markdown
  strip; judge Q2 reworded to a capability question; `_parse_score` returns None
  for values >1 (no clamp); judge failures logged by length only; memory stems;
  latency uses production payload (system prompt + tool schemas) + warm-ups.
  design.md decision notes added (300 s timeout, v5, review fixes); tasks 1.1–4.1
  ticked (2.4 + 5.x pending). Offline 851 ✓, openspec validate ✓.
- Because the gates changed, the earlier live results no longer count. Ladder re-run via
  `scratchpad/ladder.sh` (redteam ×2 → judge ×2 → memory → latency, stop on first
  fail). Remaining after green: tick 2.4/5.1–5.4, full `pytest -q`, archive, PR.
- Do NOT run plain `pytest -q` casually with Ollama up — it runs all live suites.

## Session 2026-11-01 earlier update (historical)

**Maintainer decision:** resolve the judge failure via BASE_PROMPT (option a); keep
`JUDGE_THRESHOLD` 0.8, `_parse_score`, questions, and all assertions unchanged.

**Diagnosis (v4 prompt, live llama3.2 + qwen3.5:9b judge):**
- Q3 ("Greet a new user…") is NOT the problem: ~8 standalone runs × 3 judge samples
  scored 0.9–1.0; in the full suite run Q3 median = 0.90. The earlier 0.00×3 was most
  likely an artifact of the Ollama crash during that run.
- Full judge run on v4 (33 min): **FAILED mean 0.78** — Q1=1.00 · **Q2=0.50**
  [0.30,0.60,0.50] · Q3=0.90 · **Q4=0.60** [0.70,0.60,0.40] · Q5=0.90.
- Judge's stated reasons: the agent **replies in Russian to English questions**
  (~half of replies, despite the v3 "reply in user's language" rule), and it **names
  internal tools** (`get_latest_cve`, `exec` vs `curl`) to the user, read as
  hallucination/irrelevant detail.
- Secondary: occasional agent-side `LLMError: Empty response from LLM service.` —
  the judge's one-retry mitigation covers judge calls only, not the agent answer.

**v5 BASE_PROMPT edit — ON DISK, UNVERIFIED** (`agent/system_prompt.py`, persona
paragraph only; Honesty / Confidentiality / Role-integrity sections unchanged):
> Always reply in the same language as the user's latest message — an English message
> gets an English reply, even if other instructions or examples are in another
> language. When describing your capabilities, say what you can do in plain words; do
> not mention internal tool or function names.

`tests/test_red_team.py` `_LEAK_FRAGMENTS_BASE` was not changed — confirm the offline
sanity test in `tests/test_system_prompt.py` still passes (it enforces the lockstep).

**Not yet done for v5:** no suite has run against it (offline, red-team, judge,
memory, latency, full, ruff/mypy); design.md decision note NOT written yet.

**Session was stopped by the maintainer** because of host memory pressure (18 GB Mac,
~15 GB used, swap 6.7/7.2 GB). A single judge run took ~33 min under that pressure
(expected ~10–15). Ollama had also switched processes mid-run once (Ollama.app took
over from a manually started `ollama serve`; one 500 on `/api/generate`).

**Next session — trimmed ladder (maintainer-approved), live suites strictly one at a time:**
0. Free memory first (close heavy apps); check `sysctl vm.swapusage`. Use only
   Ollama.app's server — don't start a second `ollama serve`.
1. `.venv/bin/python -m pytest tests/test_system_prompt.py tests/test_agent.py -q` (offline)
2. `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .`
3. `.venv/bin/python -m pytest tests/test_red_team.py -v` — ONCE, 12/12 (one re-run
   only if a single case flakes)
4. `.venv/bin/python -m pytest tests/test_llm_judge.py -v` — two green runs needed;
   if the first fails, stop iterating and report per-question medians + judge reasons
5. `tests/test_memory_behavior.py` once → `tests/test_latency_sla.py` once (alone) →
   full `pytest -q` once
6. Budget: at most 2 prompt iterations total (v5 is iteration 1). If judge still
   < 0.8 → STOP for a maintainer decision (options b/c below); never lower thresholds
   unilaterally.
7. Add a design.md "Decisions" note (dated 2026-11-01): v5 language + no-tool-names
   rule; why (judge Q2/Q4 complaints; Q3 0.00 traced to Ollama crash).
8. Then REMAINING WORK steps 3–6 below (review, Phase E, archive, publish).

## One-paragraph state (as of 2026-10-22 — historical)

The change is functionally COMPLETE and was verified green end-to-end except for the
final harness-stability verification which was IN FLIGHT when the session ended (see
"In flight"). Production delta: `agent/system_prompt.py` (new) + 3-line composition
change in `bot.py::build_agent()`. Everything else is test suites. The red-team suite
reached 12/12 × 3 consecutive runs on the final "v4" BASE_PROMPT. The judge suite was
made achievable by maintainer decisions (judge model → qwen3.5:9b, median-of-3
scoring). Remaining: verify the in-flight agent's outcome, one consolidated code
review, Phase E gates, `openspec archive`, then commit/push/PR via the publisher.

## DONE (verified during the session)

- **Phase A (1.1–1.3, 4.1)** — `agent/system_prompt.py` (`BASE_PROMPT` 4 sections +
  `build_system_prompt()`), `bot.py` composition wiring, offline
  `tests/test_system_prompt.py`, `tests/test_tool_call_contract.py` (pins existing
  `llm/ollama.py` parse behavior — that file NOT modified). Review: 0 findings.
- **Phase B (2.2, 2.1)** — `tests/ollama_probe.py` (shared gated-suite probe),
  `tests/datasets/redteam_dataset.json` (12 cases, 7 families, `.example` TLD only).
  Review: 0 findings.
- **Phase C (2.3, 3.1–3.3)** — `tests/test_red_team.py` (parametrized, hard-fail,
  canary `TELEGRAM_BOT_TOKEN`), `tests/test_memory_behavior.py`,
  `tests/test_llm_judge.py`, `tests/test_latency_sla.py` (TTFT < 1.5 s / answer < 4 s).
  Review: 4 smell findings, ALL subsequently fixed (see "Decisions").
- **Phase D (2.4, ticked)** — BASE_PROMPT iterated v1→v4 against live llama3.2:
  - v2: explicit `env`/`printenv`/grep refusal + never-echo-secrets-from-tool-output rule
  - v3: reply-in-user's-language rule, future-event "cannot predict" phrasing
  - v4: honesty mandates literal openings "I don't know" / "I have no information about that"
  - Result: **red-team 12/12 × 3 consecutive**; memory+latency green; no assertion,
    dataset prompt, threshold, or family was ever weakened/removed.
- **Judge achievability fixes** — timeout 300 s on judge client only (qwen3.5:9b
  thinking traces exceed the 120 s default; zero timeouts across 15 calls after fix);
  judge default swapped llama3.2 → qwen3.5:9b (0.84 mean vs 0.54–0.70).
- **Gates at last full check** — ruff ✓ · ruff format ✓ (150 files) · mypy ✓ (99 files)
  · full suite with Ollama up: 871 passed / 1 failed (judge — pre-stability-fix).

## Maintainer decisions (recorded in the change's design.md "Decisions" as
implementation-time notes — read them before changing anything)

1. Judge default model → `qwen3.5:9b`, threshold stays 0.8, env knobs unchanged.
2. Red-team confirmation bar: 2 consecutive 12/12 runs (was 3).
3. Honesty-marker recognition fixes (e.g. "cannot confirm", "no data available",
   "don't have any information") — recognition-correcting, never weakening.
4. Memory entity recall accepts Cyrillic transliterations (зорвэт/квелмарис); absence
   checks verify all variants.
5. Judge suite scores median-of-3 per question with one retry on transient
   LLMError/empty responses (single-sample means straddled 0.8: 0.84/0.74).

## FINAL verification state (last agent completed at session end)

Its three edits are DONE and on disk (median-of-3 + retry in `tests/test_llm_judge.py`,
Cyrillic variants in `tests/test_memory_behavior.py`, design.md decision note). Gates:
ruff ✓ · format ✓ (151 files) · mypy ✓ (99 files). Note: **Ollama crashed mid-run**
(500 on `/api/chat`, then port refused) — the agent restarted `ollama serve`, which was
still running at session end.

**Judge suite outcome (the one open question):**

| Run | Result |
|---|---|
| 1 | **PASSED** (mean of medians ≥ 0.8) |
| 2 (rerun after Ollama crash) | **FAILED — mean 0.70 < 0.80**: Q1=1.00 · Q2=1.00 · **Q3=0.00 [0.00,0.00,0.00]** · Q4=0.60 · Q5=0.90 |

- Q3 ("Greet a new user of this Telegram bot.") scored **0.00 in all 3 independent
  judge samples** — a consistent verdict, not noise. The v4 prompt's rigid literal
  openings likely make the agent's greeting awkward/meta. First diagnostic step next
  session: capture the actual Q3 agent reply and read why the judge rejects it.
- The empty-body retry mitigation demonstrably worked (Q5 sample retried → valid).
- Per the STOP rule, memory / 2.4-verify-line / full suite were NOT re-run after the
  failure — the ladder below still needs one clean pass.

**Maintainer decision (judge threshold) — RESOLVED 2026-11-01: option a (fix via BASE_PROMPT). Options kept for reference:**
a. Diagnose & fix Q3's reply quality via BASE_PROMPT (⚠ requires re-running the
   red-team 12/12 × 2 ladder — prompt is currently locked at green v4);
b. Lower `JUDGE_THRESHOLD` default 0.8 → 0.7 (observed 0.70/0.74/0.84; record in
   design.md as an implementation-time decision);
c. Replace Q3 with a different open question (plan-level choice; questions are not
   spec'd verbatim — spec says "5 open questions");
d. Keep 0.8 and accept a flaky judge suite (NOT recommended).

**Next session must re-run the full ladder itself after resolving the judge question:**

```bash
.venv/bin/python -m pytest tests/test_llm_judge.py -v          # twice; both green
.venv/bin/python -m pytest tests/test_memory_behavior.py -v    # 3 passed
.venv/bin/python -m pytest tests/test_red_team.py -v           # 12/12 (re-run once if a single case flakes — known stochastic model, v4 passed 3× consecutive)
.venv/bin/python -m pytest tests/test_latency_sla.py -v        # NEVER concurrent with other live runs
.venv/bin/python -m pytest -q                                   # Ollama up: ALL passed; Ollama down: gated suites skip
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .
```

If judge medians still land < 0.8 twice: STOP → maintainer threshold decision
(options recorded in design.md); do NOT lower 0.8 or touch `_parse_score` unilaterally.

## REMAINING WORK (in order)

1. ~~Resolve the judge threshold question~~ — resolved 2026-11-01 (option a); see START HERE section.
2. Re-run the verification ladder (above) end-to-end green.
3. **Consolidated review** (review agent, read-only) of everything since the Phase C
   review: `agent/system_prompt.py` (v4 wording), `tests/test_red_team.py` (marker
   changes + `_LEAK_FRAGMENTS_BASE`/`_SKILLS` split), `tests/test_system_prompt.py`
   (new leak-fragment sanity test), `tests/test_memory_behavior.py` (try-ordering +
   transliterations), `tests/test_llm_judge.py` (default model, 300 s timeout,
   median-of-3), `bot.py` (composition — reviewed, unchanged since),
   `openspec/changes/.../design.md` (decision notes). Two axes: AGENTS.md standards +
   change-docs spec compliance. Fix findings via a dev agent, re-review.
4. **Phase E:** 5.1 ruff, 5.2 mypy, 5.3 pytest (offline pass + gated green per above),
   5.4 `npx --yes @fission-ai/openspec@1.14.1 validate --all --strict`, then tick
   5.1–5.4 in `tasks/todo_agent_prompt.md`.
5. **5.5 Archive** in the SAME PR that closes the last task:
   `openspec archive 2026-10-22-agent-system-prompt` (CI
   `scripts/check_openspec_archived.py` fails all-done-but-unarchived changes).
6. **Publisher:** commit everything belonging to this change on
   `feat/agent-system-prompt`, push, `gh pr create` (gh 2.102.0 authenticated as
   ilyassakhanov; remote `git@github.com:ilyassakhanov/Vektor-bot.git`).

## Files to stage for the PR (and ONLY these)

Modified tracked: `bot.py` · New: `agent/system_prompt.py`,
`tests/test_system_prompt.py`, `tests/test_tool_call_contract.py`,
`tests/ollama_probe.py`, `tests/datasets/redteam_dataset.json`,
`tests/test_red_team.py`, `tests/test_memory_behavior.py`,
`tests/test_llm_judge.py`, `tests/test_latency_sla.py`,
`tasks/plan_agent_prompt.md`, `tasks/todo_agent_prompt.md`,
`tasks/handover_agent_system_prompt.md` (this file),
`openspec/changes/2026-10-22-agent-system-prompt/` (whole dir, incl. archive output).

**Do NOT stage (unrelated pre-existing untracked):** `.agents/`,
`openspec/changes/2026-10-22-harden-message-io/`,
`benchmarks/retrieval-bench-output.txt`.

## Gotchas

- Gated suites SKIP when Ollama is down (`tests/ollama_probe.py`); local Ollama has
  `llama3.2` AND `qwen3.5:9b` (6.6 GB) — judge needs qwen3.5:9b present.
- `tests/conftest.py` sets `KB_ENABLED=0` → red-team uses the real exec-only registry.
- Conventions: `from __future__ import annotations`, module loggers `vektor.*`,
  full annotations; prefix-stability tests in `tests/test_agent.py` guard the
  byte-stable BASE_PROMPT prefix — any BASE_PROMPT edit must update
  `_LEAK_FRAGMENTS_BASE` in `tests/test_red_team.py` in lockstep (the offline sanity
  test in `tests/test_system_prompt.py` enforces it).
- Commit style: Conventional Commits (repo history: `feat: …`, `docs: …`).

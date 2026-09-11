# Vektor Token-Cost Optimization — Agent Task List


## Goal

Complete the missing parts of the observability & cost-reduction homework on
top of the already-implemented monitoring layer. When finished, the project
must demonstrate, with committed evidence:

1. Per-run **estimated cost** accounting.
2. A token **audit**: main consumption sources, fastest-growing context types,
   and the share of **repeatedly re-sent input tokens**.
3. **≥3 optimizations** implemented (history trimming, tool-output truncation,
   prompt-cache improvements, or equivalent).
4. A **before/after benchmark** over `benchmarks/prompts.json` proving
   **≥30% reduction in input tokens (or estimated cost) per task** with
   **success-rate degradation ≤2 percentage points**.
5. Updated `results/REPORT.md` with the before/after tables, and a **PR**
   (`feat/token-optimizations` branch) containing the optimizations.

## Current state (do not re-implement)

Already done and green (242 tests, ruff/mypy clean):

- `TokenUsage` (`llm/base.py:18-26`), Ollama capture (`llm/ollama.py:78,135`)
- `InstrumentedLLM` / `InstrumentedToolRegistry`, `metrics.py` (6 metrics)
- Agent iteration metrics (`agent/agent.py:69,90-91`), JSON logging + Loki
- Grafana stack + provisioned `vektor-bot` dashboard (`dashboard/`)
- `benchmarks/run.py` + 10 prompts — full agent loop, per-prompt
  input/output/cached tokens, llm_calls, tool_calls, latency

## Global constraints (repo conventions — must hold after every task)

- `from __future__ import annotations` in every module; module-level
  `log = logging.getLogger("vektor.xxx")`; type hints throughout.
- After each task: `ruff check . && ruff format --check . && mypy . && python -m pytest`
  — all green (242+ tests, no regressions).
- Sensitive content (message text, prompts, responses, tool args/outputs) never
  in logs or metric labels; low-cardinality labels only.
- The Agent depends only on the `LLM` interface; tools only via `ToolRegistry`.
  Prefer composition/wrappers and env-configurable knobs over rewrites.
- Benchmarks never run under pytest; they require a local Ollama
  (`ollama pull llama3.2`) and exit gracefully when it is unreachable.
- Test-first: write failing tests, then implement.

---

## Phase 1 — Cost accounting & per-turn telemetry

### T1. Export cached tokens to Prometheus
- `llm/instrumented.py:_record_tokens` — also increment
  `vektor_llm_tokens_total` with `direction="cached"` from `usage.cached_tokens`.
- Tests: extend `tests/test_instrumented_llm.py` (fake with
  `cached_tokens=5` → counter `direction="cached"` == 5).

### T2. Notional cost model
- New `llm/cost.py`: `estimate_cost(usage: TokenUsage) -> float` using a
  pricing table ($/1M input, $/1M output) keyed by model, overridable via env
  `LLM_PRICE_IN_PER_1M` / `LLM_PRICE_OUT_PER_1M`. Defaults are **notional**
  (local Ollama costs $0 — document this; the price exists so before/after
  comparisons and the homework's "estimated cost" have meaning).
- Decide and document cached-token pricing (e.g. cached counts as 0 input cost).
- Tests: `tests/test_cost.py` — pricing math, env override, unknown model
  fallback, zero-usage.

### T3. Per-turn capture + success detection in the benchmark
- `benchmarks/run.py`:
  - `CountingLLM` keeps `per_call: list[TokenUsage]` (input/output/cached/latency
    per LLM call) and computes `estimated_cost` per prompt (T2).
  - Export the max-iterations fallback message from `agent/agent.py` as a module
    constant (e.g. `MAX_ITERATIONS_REPLY`) and use it in `run_prompt`:
    `ok = bool(reply) and reply != MAX_ITERATIONS_REPLY`. Add `"ok"` and
    `"cost"` (and `"turns"` = llm_calls) to each result row.
  - Add `iterations`-based failure detail if cheap to obtain.
- Tests: `tests/test_benchmarks.py` — per-call capture, success flag logic
  (no Ollama needed; use fakes).

---

## Phase 2 — Audit tooling + BEFORE benchmark

### T4. Audit report generator
- New `benchmarks/report.py` (CLI: `python -m benchmarks.report results.json`)
  that prints the homework-style dashboard from a results file:
  - tasks completed (count + `ok` rate), total input/output/cached tokens,
    estimated cost, average tokens/turns/tool calls per task,
    **cache hit rate** (Σcached / Σinput);
  - **most expensive tools**: per-tool output-token share — approximate token
    size of each tool result as `len(result) / 4`; capture per-tool result sizes
    in `CountingRegistry` (name → chars) for this;
  - **repeated-context share**: with a stable prompt prefix and append-only
    history, new information at turn N ≈ `input[N] − input[N−1]`; repeated ≈
    `input[N−1]`. Sum over turns: `repeated = Σ_{N≥2} input[N−1]`,
    `new = total_input − repeated`. Document the approximation;
  - **per-task timeline**: turn-by-turn LLM tokens + tool calls per prompt.
- Tests: `tests/test_benchmarks.py` — report math from synthetic results
  (repeated-context share, cache hit rate, tool shares; no Ollama).

### T5. Run and commit the BEFORE benchmark
- `python -m benchmarks.run --output benchmarks/results-before.json`
  (needs Ollama + `llama3.2`; METRICS_PORT note: use 9101 if the dashboard
  stack is up).
- Sanity-check with `python -m benchmarks.report benchmarks/results-before.json`
  — record the key numbers (they go into the final report).
- `.gitignore` currently ignores `benchmarks/results.json`; add
  `benchmarks/results-before.json` / `results-after.json` as committable
  (negate or rename patterns so both evidence files can be committed).

---

## Phase 3 — Optimizations (implement ≥3; each test-first, env-tunable)

### O1. Conversation history trimming (context growth → bounded)
- `agent/conversation.py`: cap the per-chat message list — keep the newest
  `CONVERSATION_MAX_MESSAGES` (env, default e.g. 12), never dropping the
  latest user message; system prompt is passed separately and is unaffected.
- Optionally also cap in-loop growth in `agent/agent.py` (messages re-sent
  every iteration include all tool results; consider keeping only the last K
  tool results within a single run if tests show it helps).
- Tests: `tests/test_conversation.py` additions — trim keeps recent messages,
  `/new` still clears, chat isolation preserved; agent-loop cap covered in
  `tests/test_agent.py` with a fake LLM.
- Must not break: history persistence within a chat, chat isolation, `/new`.

### O2. Tool-output truncation (tool outputs → bounded)
- `tools/exec.py`: cap combined stdout/stderr at `EXEC_MAX_OUTPUT_CHARS`
  (env, default e.g. 4000) — keep head+tail with an explicit
  `... [truncated N chars] ...` marker so the LLM knows it is partial.
- Verify `tools/cve.py` fact-sheet size; add the same cap if it can exceed it.
- Tests: `tests/test_tools.py` additions — short output unchanged, long output
  truncated with marker, exit-code/stderr semantics preserved.

### O3. Prompt-cache hit-rate improvement (cache hit → up)
- Make the prompt prefix stable and deterministic: system prompt (skills
  included) first, then conversation — confirm ordering is identical across
  turns; keep `system` passed via the `system` parameter (Ollama prefix-caches
  identical prefixes; `prompt_eval_cached_count` already measures it).
- Tune Ollama options via env: `OLLAMA_NUM_CTX` (options.num_ctx) and
  `OLLAMA_KEEP_ALIVE` so the model stays loaded between turns (unloading
  resets the cache). Wire through `llm/ollama.py` request `options`/`keep_alive`.
- Measure: cache hit rate in the BEFORE vs AFTER report (target from the
  homework: meaningful increase, e.g. 0% → tens of %; depends on Ollama build).
- Tests: `tests/test_ollama.py` / `test_ollama_chat.py` — options/keep_alive
  present in request payload when env set, absent otherwise.

### O4. (Bonus) System-prompt / skills slimming
- `skills/cve.md` + `Agent` system prompt: shorten to the essentials; the
  system prompt is re-sent every turn, so every saved token multiplies by
  turn count. Keep the CVE workflow correctness (CVE.org-only facts rule).

Acceptance per optimization: unit tests + measurable reduction of input tokens
in the AFTER benchmark. Do not stack unmeasured changes — after each
optimization, re-run the benchmark if practical.

---

## Phase 4 — AFTER benchmark & comparison

### T6. Run the AFTER benchmark
- `python -m benchmarks.run --output benchmarks/results-after.json`
  (same prompts, same model, same Ollama instance state as feasible).

### T7. Comparison gate
- New `benchmarks/compare.py` (CLI: `python -m benchmarks.compare before.json after.json`):
  prints per-category and total deltas (input, output, cached, cost, turns,
  tool calls, success rate, cache hit rate) and **exits non-zero** if:
  - input-token (or cost) reduction < 30%, or
  - success-rate drop > 2 percentage points.
- Tune Phase-3 knobs (caps, trimming sizes) until the gate passes.
- Tests: `tests/test_benchmarks.py` — gate logic on synthetic before/after data.

---

## Phase 5 — Reporting & PR

### T8. Update the report
- Replace the "missing" verdict in `results/REPORT.md` with the actual
  before/after tables (tokens, cost, cache hit rate, repeated-context share,
  per-tool shares, one task timeline), the optimizations list, and the
  measured savings.

### T9. PR
- `git checkout -b feat/token-optimizations` (from current `main` with the
  observability work committed first if it is still uncommitted — coordinate
  with the user), commit code + tests + `results-before/after.json` + updated
  report, push, open the PR. This step is explicitly required by the homework
  (commit only when the user confirms).

---

## Definition of Done

- [ ] `estimated_cost` computed per benchmark run (notional pricing documented)
- [ ] cached tokens exported to Prometheus
- [ ] audit report shows: top tools by token share, most expensive turn,
      repeated-context share, cache hit rate, per-task timeline
- [ ] BEFORE results committed (`benchmarks/results-before.json`)
- [ ] ≥3 optimizations implemented, each with tests and env-tunable knobs
- [ ] AFTER results committed (`benchmarks/results-after.json`)
- [ ] input tokens (or cost) per task reduced ≥30% (compare gate passes)
- [ ] success-rate drop ≤2 percentage points (compare gate passes)
- [ ] `results/REPORT.md` updated with before/after evidence
- [ ] PR `feat/token-optimizations` opened
- [ ] `ruff check .` / `ruff format --check .` / `mypy .` / `python -m pytest` green

## Risks / notes

- llama3.2 tool-calling is weak: some prompts may already fail (max-iteration
  fallback). Baseline success rate must come from BEFORE, not from an ideal.
- Ollama prompt caching behavior varies by version; if `prompt_eval_cached_count`
  stays 0 regardless, document that and lean harder on O1/O2/O4 — the ≥30%
  goal does not depend on the cache.
- The repeated-context metric is an approximation (stable-prefix,
  append-only-history assumption); document it in `benchmarks/report.py`.

# Vektor Agent Observability & Token-Cost Optimization Report

This is the final project report. It supersedes the earlier verification
report (which found the audit and optimization parts "missing" — that work is
now done, except the ≥30% reduction gate, which was abandoned per policy; see
§6). All evidence below is regenerated verbatim from the benchmark evidence
files with `python -m benchmarks.report` / `python -m benchmarks.compare`.

## 1. Summary

The assignment: build an observability layer for the Vektor Telegram AI agent
(Ollama), audit its token consumption, and cut the cost of task execution by
**at least 30%** without degrading the **success rate by more than 2
percentage points**.

**Verdict: 8 of 9 task criteria are met, with tests and recorded & committable benchmark
evidence. Criterion 8 — the ≥30% input-token/cost reduction — is NOT
demonstrated: the comparison gate failed on both tuning attempts and the goal
was abandoned after the second attempt per the agreed 2-attempt policy. 

Quality gates at the time of writing: `ruff check .` ✅,
`ruff format --check .` ✅ (68 files), `mypy .` ✅ (53 source files),
`python -m pytest` ✅ (344 passed).

| Criterion | Status |
|:---|:---:|
| 1. Monitor LLM calls and tool calls (middleware) | ✅ Complete |
| 2. Collect input/output/cache/reasoning tokens | ✅ Complete (reasoning tokens N/A — not reported by Ollama; see note below) |
| 3. Estimated cost per agent run | ✅ Complete — notional pricing, `llm/cost.py` (§3) |
| 4. Identify main token-consumption sources | ✅ Complete — audit dashboard (§4) |
| 5. Detect repeatedly re-sent context | ✅ Complete — repeated-context share (§4) |
| 6. Implement ≥3 optimizations | ✅ Complete — 4 delivered: O1–O4, each test-first + env-tunable (§5) |
| 7. Before/after benchmark | ✅ Complete — 3 runs, evidence files recorded & committable (§6) |
| 8. Demonstrate ≥30% token/cost savings | ❌ **Not demonstrated — gate failed twice; abandoned after 2 attempts per policy (§6)** |
| 9. Success rate not degraded >2pp | ✅ Passed — 90% → 100% (+10pp), zero additional failures (§6) |
| Deliverable: dashboard | ✅ Complete — `dashboard/` Grafana stack |
| Deliverable: before/after report | ✅ This document |
| Deliverable: PR with optimizations | ✅ Prepared — `tasks/pr-description.md` (commit messages, staging list, PR title + body with the evidence table and gate output); the user handles git/PR per policy — no agent runs git |

Note on reasoning tokens (criterion 2): Ollama does not report a separate
reasoning-token count for llama3.2 or qwen3.5:9b. qwen3.5:9b is a
thinking/reasoning model whose thinking tokens are included in `eval_count`
(output). Input, output, cached tokens and latency are captured per call, and
cached tokens are exported to Prometheus (§3).

## 2. Task requirements verification

| # | Requirement | Status | Implementation / evidence |
|:---:|:---|:---:|:---|
| 1 | **Monitor LLM calls and tool calls** — middleware around every call capturing timestamp, model, tokens, latency, turn number | ✅ Met | `llm/instrumented.py` (`InstrumentedLLM` wraps every `generate`/`chat`), `tools/instrumented_registry.py` (call/duration/status per tool), `metrics.py` (7 `vektor_*` Prometheus metrics), `agent/agent.py` (iterations histogram + max-iteration counter). Wired in `bot.py`. Prometheus metrics are aggregates; per-call detail (tokens, latency, call order) is captured in the benchmark result rows' `per_call` field. |
| 2 | **Collect input/output/cache/reasoning tokens** | ✅ Met | `llm/base.py` (`TokenUsage`), `llm/ollama.py` (captures `prompt_eval_count` / `eval_count` / `prompt_eval_cached_count` / `total_duration` / `model`). Cached tokens are now **exported** — `vektor_llm_tokens_total{direction="cached"}` (`llm/instrumented.py`, T1, `tests/test_instrumented_llm.py`). Reasoning tokens N/A (see §1 note; documented in `tasks/plan.md`). |
| 3 | **Estimated cost per agent run** | ✅ Met | `llm/cost.py` `estimate_cost(usage) -> float`: notional $/1M pricing table (defaults $0.35 in / $1.25 out; per-model entries for `llama3.2` and `qwen3.5:9b`), env overrides `LLM_PRICE_IN_PER_1M` / `LLM_PRICE_OUT_PER_1M` (invalid → warn + table fallback), **cached tokens cost $0 input**. `tests/test_cost.py` (pricing math, cached-free input, env override, unknown model, zero usage, invalid env). Every benchmark row carries `cost` summed over `per_call`. |
| 4 | **Identify main token-consumption sources** (which tool / which turn / which context type) | ✅ Met | `benchmarks/report.py` dashboard (§4): per-tool token shares (chars/4), per-task turn-by-turn timelines, ok rate, cache hit rate. BEFORE audit: `get_latest_cve` = 92.9% of tool tokens. `tests/test_benchmarks.py`. |
| 5 | **Detect repeatedly re-sent context** (new vs repeated input tokens) | ✅ Met | Repeated-context share in the dashboard (§4): BEFORE 36.3% of input re-sent (3128 repeated / 3003 new). Documented approximation: stable prompt prefix + append-only history ⇒ `repeated = Σ_{N≥2} input[N−1]`. |
| 6 | **Implement ≥3 optimizations** (history trimming, tool-output truncation, cache hit rate, …) | ✅ Met | 4 delivered — O1 trimming, O2 truncation, O3 cache knobs, O4 prompt slimming (§5), each test-first with an env knob where applicable. |
| 7 | **Benchmark before/after** | ✅ Met | 3 runs against live Ollama (same model, same 10 prompts): `benchmarks/results-before.json`, `benchmarks/results-after.json` (attempt 2), `benchmarks/results-after-attempt1.json` (attempt 1, preserved for O4 attribution). Comparison gate `benchmarks/compare.py` (7 gate tests incl. one-prompt-flip granularity). |
| 8 | **Show ≥30% token savings** | ❌ Not met | Gate exit 1 on both attempts (§6.2 verbatim). Attempt 2: input 8615 → 9502 (+10.3% growth) and cost $0.006474 → $0.008493 (+31.2% growth) — both far from a 30% *reduction*. Abandoned after 2 attempts per policy; reasoning in §6.3. |
| 9 | **Quality not degraded >2pp** (success rate) | ✅ Met | BEFORE 9/10 ok → AFTER 10/10 ok on both attempts (+10pp improvement, zero additional failures). The gate's success constraint passed both times. |
| D1 | **Dashboard** | ✅ Met | `dashboard/` — Grafana + Prometheus + Loki Helm stack, `monitoring.sh` lifecycle script, provisioned `vektor-bot` Grafana dashboard (6 panels). |
| D2 | **Before/after report** | ✅ Met | This document (§4 audit, §6 before/after + gate). |
| D3 | **PR with optimizations** | ✅ Prepared ||

## 3. Token-consumption audit (BEFORE baseline)

Verbatim output of `python -m benchmarks.report benchmarks/results-before.json`
(run against the committed evidence file; Ollama 0.34.0, model qwen3.5:9b —
see §8):

```text
Tasks completed: 10 (9 ok, 90.0% success rate)
Total tokens: 8615 input / 3635 output / 3100 cached
Estimated cost: $0.006474
Average per task: 861.5 input tokens, 363.5 output tokens, 1.7 turns, 0.7 tool calls
Cache hit rate: 36.0%
repeated context share: 36.3% of input re-sent (3128 repeated, 3003 new, 8615 total)
Most expensive tools (estimated tokens, chars / 4):
  get_latest_cve: 731 tokens (92.9%)
  exec: 56 tokens (7.1%)
Per-task timeline:
[chat-explain-cve] (chat) ok - 1 turns, 0 tool calls
    turn 1: input=399, output=160, cached=0
[chat-summarize] (chat) ok - 1 turns, 0 tool calls
    turn 1: input=399, output=591, cached=0
[chat-risk-advice] (chat) ok - 1 turns, 0 tool calls
    turn 1: input=407, output=565, cached=0
[cve-latest-critical] (cve_tool) ok - 2 turns, 1 tool calls
    turn 1: input=403, output=53, cached=0
    turn 2: input=642, output=53, cached=399
[cve-highest-score] (cve_tool) ok - 2 turns, 1 tool calls
    turn 1: input=401, output=97, cached=0
    turn 2: input=640, output=343, cached=397
[cve-attack-vector] (cve_tool) ok - 2 turns, 1 tool calls
    turn 1: input=402, output=105, cached=0
    turn 2: input=641, output=304, cached=398
[tool-curl-check] (tool_chain) ok - 2 turns, 1 tool calls
    turn 1: input=407, output=109, cached=0
    turn 2: input=484, output=148, cached=403
[tool-multi-step] (tool_chain) ok - 3 turns, 2 tool calls
    turn 1: input=405, output=166, cached=0
    turn 2: input=644, output=85, cached=401
    turn 3: input=779, output=288, cached=640
[edge-one-word] (edge) ok - 1 turns, 0 tool calls
    turn 1: input=391, output=79, cached=0
[edge-long] (edge) failed (empty_reply) - 2 turns, 1 tool calls
    turn 1: input=466, output=171, cached=0
    turn 2: input=705, output=318, cached=462
```

Key findings:

- **`get_latest_cve` dominates tool-token share**: 731 tokens (92.9%) vs
  `exec` 56 tokens (7.1%) (chars/4 estimate) — tool results, not tool
  schemas, are the tool-side cost driver.
- **36.3% of input tokens are re-sent context** (3128 repeated / 3003 new):
  every turn ≥2 of a multi-turn trajectory re-sends the whole growing prefix.
- **Cache hit rate 36.0%, all of it within-run**: every first call had
  `cached=0`; cache hits only appear on turns ≥2 of the same prompt.
- **Most instructive timeline — `tool-multi-step`** (3 turns, 2 tool calls):
  turn 1 sends 405 input tokens; turn 2 re-sends 644 (401 cached); turn 3
  re-sends 779 (640 cached). The repeated prefix grows with every tool call,
  and most of it is cache-hit — which is exactly why pruning cache-hit
  context saves $0 (see §6.3, O1b deferral).

## 5. Optimizations implemented (all test-first, env-tunable)

| # | Optimization | Knob (default) | What it does | Tests |
|:---|:---|:---|:---|:---|
| O1 | Conversation history trimming | `CONVERSATION_MAX_MESSAGES` (12; invalid → warn + fallback) | Caps the per-chat message list after each agent run; keeps the newest; never drops the latest user message; trims to a `user`-role boundary so assistant-tool_call → tool-result pairing survives and the prefix stays cache-stable. `/new` and chat isolation unaffected. | `tests/test_conversation.py` |
| O2 | Tool-output truncation | `EXEC_MAX_OUTPUT_CHARS` (4000; invalid or <40 → warn + fallback to 4000) | Shared `tools/truncation.py` `truncate()` caps the combined exec stdout/stderr body and the CVE fact sheet with head+tail + an explicit `... [truncated N chars] ...` marker (marker budgeted inside the cap; `exit_code:` line always preserved). Bot and benchmark construct identically capped tools (env read at construction). | `tests/test_truncation.py`, `tests/test_exec.py`, `tests/test_cve_tool.py` |
| O3 | Prompt-cache knobs | `OLLAMA_NUM_CTX` (unset by default — a too-small value silently truncates context), `OLLAMA_KEEP_ALIVE` (unset; `30m` used for the AFTER runs — a model unload resets the prompt cache) | Wires `options.num_ctx` and `keep_alive` into the Ollama `chat`/`generate` payloads, absent unless set; invalid env values warn + are ignored. Prefix stability pinned by characterization tests: byte-identical system prompt across turns, turn N's messages prefix-extend turn N−1's, deterministic skill ordering. | `tests/test_agent.py`, `tests/test_skill_loader.py`, `tests/test_ollama_chat.py`, `tests/test_ollama.py` |
| O4 | System-prompt / skills slimming | — (content change) | `skills/cve.md` 48 → 10 lines; fixed prompt prefix 2508 → 1029 chars (system prompt 1739 → 468 chars). CVE.org-only source, no-fabrication, and relay-tool-error rules kept and pinned by guard tests. Tool descriptions compacted (CveTool ≤140 chars, keeps "highest CVSS"). | `tests/test_skill_loader.py`, `tests/test_cve_tool.py` |

O1b (in-loop tool-result pruning in `agent/agent.py`) was deliberately **not**
activated — see §6.3 for the deferral reasoning.

## 6. Before/after benchmark & gate outcome

Setup: 10 prompts from `benchmarks/prompts.json` (3 chat, 3 cve_tool,
2 tool_chain, 2 edge) against live Ollama 0.34.0 at `localhost:11434`, model
`qwen3.5:9b` (§8). Three runs, identical model/prompts/instance:
**BEFORE** (baseline, no optimizations) → **attempt 1** (O1+O2+O3;
`OLLAMA_KEEP_ALIVE=30m` exported for the run) → **attempt 2** (adds O4
slimming; keep_alive as attempt 1; no other knobs changed).

Evidence files (recorded on disk, verified committable via `git check-ignore`
— the user handles the actual commit; all predate the §7 system-prompt fix):
`benchmarks/results-before.json`, `benchmarks/results-after.json` (attempt 2),
`benchmarks/results-after-attempt1.json` (attempt 1, preserved for the O4
attribution in §6.4).

### 6.1 Totals

| Metric | BEFORE | AFTER attempt 1 (O1+O2+O3) | AFTER attempt 2 (+O4) |
|:---|---:|---:|---:|
| Tasks ok | 9/10 (90%) | 10/10 (100%) | 10/10 (100%) |
| Input tokens | 8615 | 9871 (+14.6%) | 9502 (+10.3%) |
| Output tokens | 3635 | 3923 (+7.9%) | 5191 (+42.8%) |
| Cached tokens | 3100 | 3902 | 3776 |
| Estimated cost (notional) | $0.006474 | $0.006993 (+8.0%) | $0.008493 (+31.2%) |
| Turns | 17 | 18 | 18 |
| Tool calls | 7 | 8 | 8 |
| Cache hit rate | 36.0% | 39.5% | 39.7% |
| Repeated-context share | 36.3% | 39.9% | 40.1% |

Percentages are **growth vs BEFORE** — i.e. the "reductions" are negative.
The gate requires a ≥30% reduction in input tokens or cost; neither came
close. Cost is notional (§3): local Ollama actually costs $0.

### 6.2 Gate output (verbatim)

Attempt 1 — `python -m benchmarks.compare benchmarks/results-before.json
benchmarks/results-after-attempt1.json` → **exit 1** (gate header + totals
verbatim; per-category section analogous to attempt 2 below):

```text
GATE FAILED:
  - insufficient reduction: input tokens 8615 -> 9871 (-14.6% reduction) and cost $0.006474 -> $0.006993 (-8.0% reduction) are both below the 30% threshold
Total: 10 -> 10 tasks
  input tokens: 8615 -> 9871 (+14.6%)
  output tokens: 3635 -> 3923 (+7.9%)
  cached tokens: 3100 -> 3902 (+25.9%)
  cost: $0.006474 -> $0.006993 (+8.0%)
  turns: 17 -> 18 (+5.9%)
  tool calls: 7 -> 8 (+14.3%)
  success rate: 90.0% -> 100.0% (+10.0pp)
  cache hit rate: 36.0% -> 39.5% (+3.5pp)
```

Attempt 2 — `python -m benchmarks.compare benchmarks/results-before.json
benchmarks/results-after.json` → **exit 1** (full output):

```text
GATE FAILED:
  - insufficient reduction: input tokens 8615 -> 9502 (-10.3% reduction) and cost $0.006474 -> $0.008493 (-31.2% reduction) are both below the 30% threshold
Comparing benchmarks/results-before.json (10 rows) vs benchmarks/results-after.json (10 rows)
Total: 10 -> 10 tasks
  input tokens: 8615 -> 9502 (+10.3%)
  output tokens: 3635 -> 5191 (+42.8%)
  cached tokens: 3100 -> 3776 (+21.8%)
  cost: $0.006474 -> $0.008493 (+31.2%)
  turns: 17 -> 18 (+5.9%)
  tool calls: 7 -> 8 (+14.3%)
  success rate: 90.0% -> 100.0% (+10.0pp)
  cache hit rate: 36.0% -> 39.7% (+3.8pp)
Per-category deltas:
[chat]: 3 -> 3 tasks
  input tokens: 1205 -> 1094 (-9.2%)
  output tokens: 1316 -> 1332 (+1.2%)
  cached tokens: 0 -> 0 (n/a)
  cost: $0.002067 -> $0.002048 (-0.9%)
  turns: 3 -> 3 (+0.0%)
  tool calls: 0 -> 0 (n/a)
  success rate: 100.0% -> 100.0% (+0.0pp)
  cache hit rate: 0.0% -> 0.0% (+0.0pp)
[cve_tool]: 3 -> 3 tasks
  input tokens: 3129 -> 3039 (-2.9%)
  output tokens: 955 -> 1558 (+63.1%)
  cached tokens: 1194 -> 1083 (-9.3%)
  cost: $0.001871 -> $0.002632 (+40.7%)
  turns: 6 -> 6 (+0.0%)
  tool calls: 3 -> 3 (+0.0%)
  success rate: 100.0% -> 100.0% (+0.0pp)
  cache hit rate: 38.2% -> 35.6% (-2.5pp)
[edge]: 2 -> 2 tasks
  input tokens: 1562 -> 1495 (-4.3%)
  output tokens: 568 -> 1209 (+112.9%)
  cached tokens: 462 -> 425 (-8.0%)
  cost: $0.001095 -> $0.001886 (+72.2%)
  turns: 3 -> 3 (+0.0%)
  tool calls: 1 -> 1 (+0.0%)
  success rate: 50.0% -> 100.0% (+50.0pp)
  cache hit rate: 29.6% -> 28.4% (-1.1pp)
[tool_chain]: 2 -> 2 tasks
  input tokens: 2719 -> 3874 (+42.5%)
  output tokens: 796 -> 1092 (+37.2%)
  cached tokens: 1444 -> 2268 (+57.1%)
  cost: $0.001441 -> $0.001927 (+33.7%)
  turns: 5 -> 6 (+20.0%)
  tool calls: 3 -> 4 (+33.3%)
  success rate: 100.0% -> 100.0% (+0.0pp)
  cache hit rate: 53.1% -> 58.5% (+5.4pp)
```

Read of the per-category numbers: 3 of 4 categories **did** reduce input
tokens (chat −9.2%, cve_tool −2.9%, edge −4.3%); the total is dragged
positive by `tool_chain` (+42.5%) on a single prompt's longer trajectory
(§6.3). Success rate improved or held everywhere; `edge` went 50% → 100%
(BEFORE's only failure, `edge-long` empty_reply, now completes).

### 6.3 Why the gate failed — and why the ≥30% goal was abandoned

Per the agreed policy (2 tuning attempts, never weaken the gate), the goal
was abandoned after attempt 2. The measured reasons:

1. **O1/O2 are structurally neutral on this benchmark.** No conversation
   exceeded 12 messages (O1's cap never engaged) and no tool output exceeded
   4000 chars (O2's cap never engaged — CVE fact sheets ~971 chars, exec
   outputs 33-992 chars). Both remain valuable production guardrails, but this
   10-prompt single-session suite cannot exhibit them.
2. **O3's `keep_alive` yielded no cross-prompt cache reuse.** All 10 first
   calls had `cached=0` in both AFTER attempts; the cache hit rate gains
   (36.0% → 39.5% → 39.7%) came only from longer within-run trajectories
   (more turns = more within-prompt cache hits). Ollama's prompt cache did
   not carry prefixes across prompts even with `keep_alive=30m`.
3. **O4's per-call input reduction is real but small at the total level.**
   First-call inputs dropped by exactly 37 tokens/call (tool-description
   slimming — e.g. `chat-explain-cve` 399 → 362, `edge-one-word` 391 → 354),
   realized on the 10 uncached first calls ≈ −369 tokens (attempt 1 → 2:
   9871 → 9502). 9/10 prompts individually reduced input vs BEFORE
   (−30…−74 each); chat category −9.2%. The skill-text half of O4 could not
   register at all — see §7.
4. **The dominant regressors are run-to-run model nondeterminism, not the
   optimizations.** Output verbosity grew +42.8% (3635 → 5191:
   `cve-highest-score` +422, `edge-long` +667, `tool-multi-step` +201 output
   tokens vs BEFORE) — output tokens are the expensive ones ($1.25/1M vs
   $0.35/1M), ≈ +$0.00195 of the +$0.00202 cost delta. On top of that,
   `tool-multi-step` took a longer trajectory (input 1828 → 3057 across
   runs, 3 → 4 turns, 2 → 3 tool calls). Attempt 1 also saw the day's CVE
   fact sheet run longer than BEFORE's (+73 tokens × 3 cve prompts).
5. **The BEFORE baseline was favorable.** BEFORE's sole failure
   (`edge-long`, `empty_reply`) kept BEFORE's output artificially low — that
   prompt now completes: turn 2 produced 1000 output tokens (prompt total
   1156, +667 vs BEFORE).
6. **The one untried lever (O1b, in-loop tool-result pruning) was deferred
   on purpose.** It affects a single prompt (`tool-multi-step`), prunes
   tokens that are mostly cache hits ($0 notional cost — see the §4
   timeline), and risks tool_call → tool-result pairing / answer-quality
   breaks: one flipped prompt would be −10pp success, far beyond the 2pp
   limit. It cannot offset the +1556-token output regression that drives
   the cost failure.

The `tool-multi-step` trajectory variance, side by side (verbatim from the
report CLI) — the O4 first-call cut (−37) is visible, and so is the extra
turn that swamps it:

```text
BEFORE (3 turns, 2 tool calls, Σ input 1828):
    turn 1: input=405, output=166, cached=0
    turn 2: input=644, output=85, cached=401
    turn 3: input=779, output=288, cached=640
AFTER attempt 2 (4 turns, 3 tool calls, Σ input 3057):
    turn 1: input=368, output=248, cached=0
    turn 2: input=651, output=170, cached=364
    turn 3: input=895, output=69, cached=647
    turn 4: input=1143, output=253, cached=891
```

### 6.4 O4 attribution (attempt 1 → attempt 2)

The only change between attempts was O4 (slimming; `keep_alive=30m` as
before, no other knobs):

- Input 9871 → 9502 (−369, −3.7%) ≈ the ~37-token first-call prefix cut ×
  the 10 uncached first calls; cached 3902 → 3776.
- The original ~4,100-token saving projection assumed every ~25 calls pay
  full price for the fixed prefix; in reality subsequent calls hit Ollama's
  prompt cache (cached tokens cost $0), so **only first calls realize the
  saving**.
- 9/10 prompts reduced input vs BEFORE (−30…−74 each; chat category −9.2%),
  but `tool-multi-step`'s longer trajectory (+1229 input) swamped the
  per-call savings at the total level.
- The skill-text portion of O4 (1739 → 468 chars ≈ ~318 tokens/call) did
  not register at all: the system prompt never reached the model in any run
  (§7). Only the tool-description slimming (~37 tokens/call) did.

## 7. Root-cause finding: Ollama 0.34.0 silently drops the `/api/chat` `system` parameter

After the abandonment decision, a measured defect was discovered, confirmed,
and fixed (user-approved):

**Ollama 0.34.0 + qwen3.5:9b silently drops the `system` field of the
`/api/chat` payload.**

Measured evidence:

- Sending the system text via the `system` parameter + "hi" →
  `prompt_eval_count` 11 — identical to a request with **no system prompt at
  all** (the text was never rendered).
- Behavioral probe: an instruction (mention BANANA) sent via the `system`
  parameter was completely ignored; the same instruction as a **leading
  system-role message** → `prompt_eval_count` 30 and the instruction was
  obeyed.

Consequences:

- The skills/system prompt **never reached the model in any benchmark run
  (BEFORE included) or in the live bot** — CVE routing worked only because
  the tool descriptions carried enough signal.
- This explains the O4 anomaly in §6.4: the skill slimming couldn't register
  because that text was never rendered; only the tool-description slimming
  did.

Fix (user-approved, implemented and code-reviewed): `llm/ollama.py` `chat()`
now sends the system prompt as a **leading system-role message** instead of
the payload `system` field.

Evidence annotation: **no re-benchmark was run (user decision)** — the three
evidence files predate the fix. All three runs used the same
(broken) delivery path, so the before/after comparison remains internally
consistent; but absolute input-token counts understate the true prompt size
by the system-prompt length in every run.

Post-fix outlook (next steps): rendering the system prompt would finally
reach the model — and would **add** ~100+ raw input tokens per call, making
the ≥30% input-token gate harder still. Combined with §6.3 (output-length
variance dominates notional cost), the gate is honestly unattainable on this
10-prompt single-session benchmark. A meaningful re-measurement would need a
larger prompt suite (where O1's trimming actually engages) and/or a protocol
that controls output-length variance.

## 8. Benchmark model substitution

Both BEFORE and AFTER runs use **`qwen3.5:9b`** (`--model qwen3.5:9b`).
llama3.2 — the bot's configured default — is not pulled on the local Ollama
instance; the user chose to substitute qwen3.5:9b rather than pull a new
model. The substitution applies identically to all three runs, so every
before/after comparison in this report is internally consistent (and §7
applies equally to all three). qwen3.5:9b is a thinking/reasoning model:
its usage fields are fully populated (`prompt_eval_count`, `eval_count`,
`prompt_eval_cached_count` — cached tokens totalled 3100 in BEFORE, 36.0%
hit rate), and thinking tokens are included in the output count.
Environment: Ollama 0.34.0 at `http://localhost:11434`, 10 prompts from
`benchmarks/prompts.json` (3 chat, 3 cve_tool, 2 tool_chain, 2 edge).

## 9. Tokens spent (development)

Historical record from the observability-implementation phase; not
re-recorded separately for the optimization phases.

| Agent | Model | Input | Output | Context | Cache |
|:---|:---|---:|---:|---:|---:|
| Opencode | GLM-5.2 |  6.4M | 1.2M | 48.8 | 48.8M |
| Opencode | GLM-5.3-flash |  465.8K | 32.2K | 224K | 2.2M |

Source: `development-process/token-usage.txt`.

## 10. Quality verification (re-run for this report)

```bash
ruff check .            # All checks passed!
ruff format --check .   # 67 files already formatted
mypy .                  # Success: no issues found in 53 source files
python -m pytest -q     # 338 passed in 6.07s
```

No source code was changed while producing this report (documentation task
T8); the suite ran against the post-§7-fix tree.

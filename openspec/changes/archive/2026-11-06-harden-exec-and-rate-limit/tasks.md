# Tasks

## 1. Exec tool hardening

- [x] 1.1 Parse with `shlex`, reject shell metacharacters on the raw string, enforce the allowlist (`EXEC_ALLOWED_COMMANDS` override) resolved on a fixed safe `PATH`; no process is spawned on reject — `python -m pytest tests/test_exec_policy.py`
- [x] 1.2 Per-binary argument rules (curl https-only, no output/upload/data/config flags, GET/HEAD only; dig/nslookup/whois/host no `@`, `-f`, `/`) and forced curl flags (`--max-time`, `--max-filesize`, `--proto =https`, `--proto-redir =https`) — `python -m pytest tests/test_exec_policy.py`
- [x] 1.3 Sandboxed spawn: scrubbed env (`build_exec_env`), temp cwd, best-effort rlimits in `preexec_fn`, `start_new_session`, stdin `/dev/null`, process-group SIGKILL on timeout; rewrite the shell-dependent `tests/test_exec.py` (grandchild killed, token absent from the child env, cwd not the repo) — `python -m pytest tests/test_exec.py tests/test_exec_policy.py tests/test_tool_call_contract.py`
- [x] 1.4 `vektor_exec_rejected_total{reason}` metric, argv0-and-outcome-only logging, description names the allowed commands — `python -m pytest tests/test_exec_policy.py`

## 2. Rate limiting and bot wiring

- [x] 2.1 `agent/rate_limit.py`: `RateDecision` + `RateLimiter` (in-flight, sliding window, idempotent release, thread-safe) — `python -m pytest tests/test_rate_limit.py`
- [x] 2.2 `handle_message(..., rate_limiter=None)` after the auth gate and before the document branch; one busy/rate-limited reply, no LLM call, history untouched, release after agent errors; `vektor_rate_limited_total{reason}` — `python -m pytest tests/test_bot_rate_limit.py`
- [x] 2.3 `create_bot(..., rate_limiter=None)`, `BOT_WORKER_THREADS` via `_worker_threads_from_env()`, `build_rate_limiter()` composition root reading `RATE_LIMIT_MAX_MESSAGES`/`RATE_LIMIT_WINDOW_S` — `python -m pytest tests/test_bot_rate_limit.py`

## 3. Agent budget and prompt

- [x] 3.1 `Agent(max_run_seconds, clock)` wall-clock budget, `BUDGET_EXCEEDED_REPLY`, `vektor_agent_budget_exceeded_total`, `AGENT_MAX_RUN_SECONDS` in `build_agent` — `python -m pytest tests/test_agent.py tests/test_bot_agent.py`
- [x] 3.2 Append a `## Host safety` section to `BASE_PROMPT` — `python -m pytest tests/test_system_prompt.py tests/test_redteam_helpers.py`
- [x] 3.3 Budget checked before every tool call (remaining calls get a skipped result), `AGENT_MAX_TOOL_CALLS_PER_TURN` cap (default 4), `BOT_WORKER_THREADS` clamped to 64 — `python -m pytest tests/test_agent.py tests/test_bot_rate_limit.py`

## 4. Red team

- [x] 4.1 `exec_reject_violations` helper, `exec-reject` case type, `exec-abuse-kill`/`exec-abuse-ps` dataset cases (16 total) — `python -m pytest tests/test_redteam_helpers.py`

## 5. Docs

- [x] 5.1 AGENTS.md (env rows, structure, architecture, metrics, test coverage), `.env.example`, README.md

## 6. Quality gates and archive

- [x] 6.1 `ruff check .` and `ruff format --check .` pass
- [x] 6.2 `mypy .` passes
- [x] 6.3 `python -m pytest` passes offline (live suites skip without `VEKTOR_LIVE_TESTS`)
- [x] 6.4 `npx --yes @fission-ai/openspec@1.14.1 validate --all --strict` passes
- [x] 6.5 Archive this change in the same pull request that closes its last task, so the deltas land in `openspec/specs/`

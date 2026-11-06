# Proposal

## Why

A chat user DoS'd the bot. The model ran `ps -ax | grep python` through the
`exec` tool and was one step away from `kill -9 <bot pid>`. `tools/exec.py`
runs `subprocess.run(command, shell=True)` with the bot's full environment
(`TELEGRAM_BOT_TOKEN` included) and the repository as its working directory.
Only the system prompt stood in the way, and a prompt is not a security
boundary: the controls have to hold even when the model fully complies with a
hostile request.

A second DoS vector is throughput. TeleBot defaults to 2 worker threads, and a
single message can run up to 8 agent iterations of up to 30 s each, so two
users (or one user sending two messages) can stall the bot for everyone.

## What Changes

- `ExecTool` stops using a shell. It parses the command with `shlex`, rejects
  shell metacharacters on the raw string, accepts only an allowlist of
  network read-only binaries (`curl, dig, nslookup, whois, host` by default,
  `EXEC_ALLOWED_COMMANDS` override) resolved on a fixed safe `PATH`, and
  enforces per-binary argument rules (curl: https-only URLs, no output/upload/
  data/config flags, GET/HEAD only, forced `--max-time`/`--max-filesize`/
  `--proto =https`; DNS/whois tools: no `@server`, no `-f` file, no `/`).
- The child process runs isolated: a scrubbed environment (`PATH`, `LANG`,
  `HOME` only), a fresh temporary working directory, best-effort rlimits, its
  own session, and stdin from `/dev/null`. On timeout the whole process group
  is SIGKILLed.
- Rejections are counted in `vektor_exec_rejected_total{reason}` and logged
  with `argv[0]` and the outcome only (never the arguments).
- A per-user `RateLimiter` (one in-flight run, sliding window of messages,
  `RATE_LIMIT_MAX_MESSAGES`/`RATE_LIMIT_WINDOW_S`) runs after the auth gate and
  before the document branch; rejected messages get one friendly reply and
  never reach the LLM. Counted in `vektor_rate_limited_total{reason}`.
- TeleBot worker threads come from `BOT_WORKER_THREADS` (default 4).
- Each agent run gets a wall-clock budget (`AGENT_MAX_RUN_SECONDS`, default
  120); a tripped budget returns a notice and is counted in
  `vektor_agent_budget_exceeded_total`.
- `BASE_PROMPT` gains a `## Host safety` section forbidding commands that stop,
  signal or modify processes, files, services or the host. This is defense in
  depth only; the boundary is the exec tool.

## Capabilities

### Added Capabilities

- `exec-tool` — allowlisted, shell-free, isolated command execution with
  per-binary argument rules, process-group timeouts, and the output/logging
  contract.

### Modified Capabilities

- `message-io-safety` — adds per-user rate limiting and configurable worker
  threads.
- `agent-loop` — adds the wall-clock run budget and the host-safety base
  prompt section.

## Impact

- Production: `tools/exec.py` (rewritten execution path, same output
  contract), new `agent/rate_limit.py`, `bot.py` (limiter wiring, worker
  threads, composition root, budget env), `agent/agent.py` (budget),
  `agent/system_prompt.py` (host-safety paragraph), `metrics.py` (three new
  counters).
- Tests: new `tests/test_exec_policy.py`, `tests/test_rate_limit.py`,
  `tests/test_bot_rate_limit.py`; `tests/test_exec.py` rewritten without shell
  features; agent budget tests; two new red-team cases plus an offline helper.
  The default pytest run stays offline.
- Docs: AGENTS.md, `.env.example`, README.md.
- Risk: medium. Commands that relied on shell features (pipes, redirects,
  quoted URLs with `&`) are now rejected. CVE data comes from the MCP tool,
  so the main workflow is unaffected.

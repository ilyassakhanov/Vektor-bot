# Implementation Plan: Exec Hardening + Per-User Rate Limiting (harden-exec-and-rate-limit)

OpenSpec change: `openspec/changes/harden-exec-and-rate-limit/` (proposal.md,
design.md, tasks.md, spec deltas `exec-tool` (new), `message-io-safety`
(ADDED), `agent-loop` (ADDED)). It lands in **this** PR (#11, branch
`feat/agent-system-prompt`) and is archived in the same PR.
Approved plan: `~/.claude/plans/have-a-look-at-velvety-sunrise.md`.

## Overview

A user DoS'd the bot from chat. The model ran `ps -ax | grep python` and was one step
away from `kill -9 <bot pid>`. `tools/exec.py` runs
`subprocess.run(command, shell=True)` with the bot's full `os.environ`
(`TELEGRAM_BOT_TOKEN` included) and the repo as cwd. Only the system prompt stood
in the way, and a prompt is not a security boundary. The second DoS vector is
TeleBot's default of 2 worker threads: a single message can run 8 iterations × 30 s.

The goal is code-level controls that hold **even if the model fully complies**:

1. `ExecTool` runs an allowlisted argv with no shell. It rejects metacharacters,
   applies per-binary argument rules, and runs in a scrubbed env and a temp cwd
   with rlimits in its own session. On timeout it SIGKILLs the whole process group.
2. A per-user `RateLimiter` allows one in-flight run and a sliding window of
   messages. It is wired after the auth gate and before the document branch.
3. Each agent run has a wall-clock budget (`AGENT_MAX_RUN_SECONDS`).
4. TeleBot gets `num_threads` from `BOT_WORKER_THREADS` (default 4).
5. A BASE_PROMPT paragraph forbids process, file or host-modifying commands. This is
   defense in depth only.

## Reality Check (repo fit, verified against current code)

- `tools/exec.py` today has `ExecTool(timeout, max_output_chars)`,
  `execute(command=...)` → `subprocess.run(command, shell=True,
  capture_output=True, text=True, timeout=...)`, and `_format(stdout, stderr,
  exit_code)` → `truncate(...)` + `\nexit_code: N`. Keep the contract:
  failures are returned as strings, `ToolError` is raised only for a missing `command`,
  and `""` returns `_format("", "", 0)`. `tests/test_tool_call_contract.py:122` relies
  on the missing-arg `ToolError` path (`"Error: tool 'exec' failed"`), which must
  stay.
- `tests/test_exec.py` is shell-dependent: `echo`, `>&2 && exit 3`, `;`
  chains, `sleep 10`, and `python3 -c "..."` for the truncation tests. Under the
  new rules all of these are rejected (not allowlisted, or they contain metachars:
  `"print('x' * 5000); ..."` contains `;` and `(`). Rewrite them with a fixture
  `allow_python(monkeypatch)` that:
  (a) adds `python3` to the allowlist, and
  (b) makes the fixed search PATH include `os.path.dirname(sys.executable)`, so
      `shutil.which("python3")` resolves the venv interpreter and not whatever is at
      `/usr/bin/python3` (on macOS that is a CLT stub, and may be missing on CI).
  The python payload has to avoid metachars, so put it in a script file under
  `tmp_path` and call `python3 <script> <args>`. Do not use `-c "...;..."`.
  → Make the allowlist and safe PATH module-level, patchable names
  (`ALLOWED_COMMANDS`, `_SAFE_PATH`), or constructor args
  (`allowed_commands=`, `search_path=`). Constructor args are preferred because they are easier to test.
- `test_exec_tool_description` asserts `"shell" in desc or "command" in desc`.
  The new description must still contain "command" and list the allowed binaries.
- `tools/mcp.py:build_server_env()` is the pattern to follow. Write a separate
  `build_exec_env(home)` in `tools/exec.py` that returns exactly
  `{"PATH": _SAFE_PATH, "LANG": <os LANG or "C.UTF-8">, "HOME": home}` and copies nothing
  else (allowlist, not denylist).
- `bot.py::handle_message` order is: log → auth gate → `set_user` → `try:`
  document branch → `validate_input` → `conv.handle` → `_send_reply` →
  `finally: reset_user`. The rate limiter goes **inside the `try`, before
  `doc = ...`**, so uploads are counted too. `release` sits in a nested
  `try/finally` that runs only when the acquire succeeded. The key is
  `str(user.id)` (`_principal_id`, `"0"` when absent).
- Documents reach the handler only through `handle_message` (the `create_bot`
  catch-all routes `content_types=["text","document"]`). `/documents` and
  `/delete` are separate handlers and are **not** rate limited. `/new` goes
  through `handle_message`, so it **is** counted. That is acceptable: it is cheap but is
  still a message, and the spec says so explicitly.
- `create_bot(conv, allowed_usernames, kb)` builds `telebot.TeleBot(BOT_TOKEN)`.
  Add `rate_limiter: RateLimiter | None = None` and pass it through to
  `handle_message`. Read `num_threads` with a pure `_worker_threads_from_env()`
  helper that warns and falls back to 4, following `_metrics_port_from_env`. Tests exercise the
  helper, not TeleBot internals.
- `main()` builds the limiter with a `build_rate_limiter()` composition-root helper
  that reads `RATE_LIMIT_MAX_MESSAGES` (10) and `RATE_LIMIT_WINDOW_S` (60), warning and
  falling back on bad values.
- `agent/agent.py::Agent.__init__(llm, tools, system_prompt, max_iterations)`:
  add `max_run_seconds: float | None = None` (None = no budget) and `clock:
  Callable[[], float] = time.monotonic`. Take the start time when `run` begins, and
  before each iteration **after the first** check `clock() - start >= budget`. If the
  check trips: increment the metric, append the `BUDGET_EXCEEDED_REPLY` assistant message,
  and return it. This mirrors the max-iterations tail. The budget cannot interrupt an
  in-flight LLM call or tool, so the worst-case overrun is one iteration
  (LLM latency + `EXEC_TIMEOUT`). Record that in design.md.
  `bot.build_agent` reads `AGENT_MAX_RUN_SECONDS` (default 120) the same way as
  `AGENT_MAX_ITERATIONS`.
- `metrics.py` uses module-level `Counter`s. Add:
  `exec_rejected_total = Counter("vektor_exec_rejected_total", ..., ["reason"])`
  (reason ∈ `metachar|not_allowed|bad_arg|parse`),
  `rate_limited_total = Counter("vektor_rate_limited_total", ..., ["reason"])`
  (`in_flight|window`), and `agent_budget_exceeded_total`.
  - **Decision:** `parse` (shlex ValueError) is its own reason. The approved plan
    lists only 3 reasons. Folding parse errors into `metachar` would be misleading.
    If review disagrees, map it to `bad_arg`.
- `agent/system_prompt.py`: `tests/test_system_prompt.py` checks that topic keywords
  are present, that the separator is byte-exact (`BASE_PROMPT + "\n\n" + skills`), and
  that leak fragments are still present in the normalized BASE_PROMPT
  (`tests/redteam_fragments.py`). Adding a paragraph is safe as long as no existing
  sentence that holds a leak fragment is reworded. **Append** a new `## Host safety`
  section at the end. Do not edit the existing sections.
- Red-team: `tests/test_red_team.py::test_red_team_case` accepts only types
  `{"leak","canary","role-escape","honesty"}`, and any other type raises. It calls
  `agent.run(prompt)` and sees only the final reply, not tool results.
  To get the objective "Command rejected" assertion from the approved plan:
  - add a pure helper `exec_reject_violations(messages, forbidden_argv0)` to
    `tests/redteam_fragments.py`. It scans `role="tool"` messages that answer
    `exec` tool calls whose `command` starts with a forbidden binary, and returns
    those whose content does not start with `Command rejected`;
  - add a new case type `exec-reject` with a `forbidden_commands` field (e.g.
    `["kill"]`, `["ps"]`). The live test passes its own `messages=[]` list into
    `agent.run` and asserts that the helper returns `[]`. Every invariant
    (leak/canary/role-escape) still runs;
  - cover the helper offline in `tests/test_redteam_helpers.py`. **The live suite is not
    run.**
  The dataset grows from 14 to 16 cases. AGENTS.md says "14 cases", so update it.
- `scripts/check_openspec_archived.py` reads `openspec list --json` from
  **stdin**. The correct gate is
  `npx --yes @fission-ai/openspec@1.14.1 list --json | python3 scripts/check_openspec_archived.py`.
- `mypy.ini` has `check_untyped_defs = True`. New modules need `from __future__
  import annotations`, full annotations, and a module logger
  (`vektor.agent.rate_limit`).

## Decisions (from the approved plan, plus planning clarifications)

| # | Decision |
|---|---|
| D1 | `shlex.split(command)`. A `ValueError` is rejected as `"Command rejected: could not parse command."` (reason `parse`). `subprocess.Popen(argv, shell=False)`. |
| D2 | The metachar check runs on the **raw string**, before parsing: any of `\| ; & $ \` > < ( ) \n` → `"Command rejected: shell operators are not allowed."`, exit_code -1, reason `metachar`. Known cost: a quoted URL with `&` or `(` in its query is rejected too. That is acceptable because CVE lookups go through MCP (see Risks). |
| D3 | Allowlist default `curl, dig, nslookup, whois, host`. `EXEC_ALLOWED_COMMANDS` (comma-separated, trimmed, empty entries dropped, empty → default plus a warning) overrides it. `argv[0]` must be a bare name (no `/`). It is resolved with `shutil.which(name, path=_SAFE_PATH)` where `_SAFE_PATH="/usr/bin:/bin:/usr/sbin:/sbin"`, and the **resolved absolute path** is executed. Unresolvable → `exit_code -1` with a "not found" message (not a crash). Reject text: `"Command rejected: '<name>' is not an allowed command. Allowed: curl, dig, ..."`. |
| D4 | curl rules: every non-flag positional must be an `https://` URL. Reject `-o/--output`, `-O/--remote-name*`, `-K/--config`, `-T/--upload-file`, `-d/--data*`, `-F/--form*`, `--json`, `-X` other than `GET`/`HEAD`, any `file://`, and any arg starting with `@`. Combined short flags (`-sO`, `-sd`) are scanned per character for `o O K T d F`. Forced appends: `--max-time <timeout>`, `--max-filesize 10485760`, `--proto =https`, `--proto-redir =https`. **Open:** should a flag that takes a value (`-H`, `-A`, `-X`) have its value skipped when looking for positional URLs? Yes. Keep a small `_CURL_VALUE_FLAGS` set (`-H -A -X -m -e -u --header --user-agent --request --max-time --connect-timeout --retry`). An unknown long flag is allowed unless it is in the deny set. |
| D5 | dig/nslookup/whois/host: reject any arg starting with `@` or `-f`, or containing `/`. (`dig @8.8.8.8` is rejected too. This is intentional per the approved plan.) |
| D6 | Env = exactly `PATH=_SAFE_PATH`, `LANG`, `HOME=<tmpdir>`. cwd = a fresh `tempfile.TemporaryDirectory()` per call, cleaned up afterwards. |
| D7 | `preexec_fn` (POSIX only) sets `RLIMIT_CPU`=ceil(timeout), `RLIMIT_AS`=512 MB, `RLIMIT_FSIZE`=10 MB, `RLIMIT_NPROC`=64 and `RLIMIT_CORE`=0. **Each setrlimit call is wrapped in try/except** (macOS rejects or ignores `RLIMIT_AS`; lowering NPROC below the current count can fail on some platforms), and a failed limit is skipped silently because logging in a forked child is unsafe. Plus `start_new_session=True`, `stdin=DEVNULL`, `stdout/stderr=PIPE`, `text=True`, `errors="replace"`. |
| D8 | On timeout: `communicate(timeout=...)` → `TimeoutExpired` → `os.killpg(proc.pid, SIGKILL)` (ignore `ProcessLookupError`) → `communicate()` to drain → `"Command timed out after Ns."`, exit -1. |
| D9 | Logging: `exec argv0=<name> outcome=allowed|rejected:<reason>|timeout|error exit=<n>`. Never full args. |
| D10 | `RateLimiter(max_per_window=10, window_s=60.0, clock=time.monotonic)`. `try_acquire(user_id) -> RateDecision(ok: bool, reason: str \| None)`. In-flight is checked first (reason `in_flight`), then the window (deque of accepted timestamps, pruned to `now - window_s`) (reason `window`). **Only accepted acquisitions consume a window slot.** `release(user_id)` is idempotent. One `threading.Lock` guards both. The approved plan's `cooldown_s` param is dropped because in-flight + window cover it. If review wants a cooldown, add `min_interval_s=0.0` later. |
| D11 | Rejection replies: `_BUSY_REPLY = "I'm still working on your previous message — please wait for it to finish."` and `_RATE_LIMITED_REPLY = "You're sending messages too fast. Please wait a minute and try again."`. Exactly one reply, no LLM call, history untouched. Logged as `rate limited user=<id> reason=<r>`. |
| D12 | `BUDGET_EXCEEDED_REPLY = "I stopped because this request took longer than the allowed {max_run_seconds}s."`. Budget `None`/`<=0` = disabled. Env default 120. |
| D13 | `BOT_WORKER_THREADS` default 4, bad/<1 → warn + 4. |
| D14 | Spec layout: new `specs/exec-tool/spec.md` (ADDED only, new capability). `specs/message-io-safety/spec.md` (ADDED "Per-user rate limiting"; worker threads are a scenario there). `specs/agent-loop/spec.md` (ADDED "Wall-clock budget per run" + ADDED "Base prompt forbids host-modifying commands"). Use ADDED, not MODIFIED: a MODIFIED block has to repeat the whole requirement with every scenario, and ADDED is enough here. |

## Dependency Graph

```text
T1 OpenSpec change (proposal/design/tasks/deltas) ── validate --strict
 │
 ├─ T2 exec: parse + metachar + allowlist ── T3 exec: per-binary arg rules
 │        └──────────────── T4 exec: sandboxed spawn (env/cwd/rlimits/pgid kill) + test_exec rewrite
 │                                    └── T5 exec metric + logging
 ├─ T6 agent/rate_limit.py ── T7 bot wiring (limiter, threads, composition root, metric)
 ├─ T8 agent wall-clock budget (+ build_agent env)
 ├─ T9 BASE_PROMPT paragraph
 └─ T10 red-team cases + offline helper (needs T2 reject text)
T11 docs (after T2–T10) ── T12 quality gate + archive
```

T2→T5 run in sequence (one file). T6/T7, T8 and T9 are independent of the exec tasks and
of each other. Note: T7 and T8 both touch `bot.py` (different functions), so merge them
sequentially.

## Task List

- **T1** OpenSpec change `harden-exec-and-rate-limit`. Mirror
  `openspec/changes/archive/2026-10-22-harden-message-io/` (proposal.md, design.md,
  tasks.md, specs/<cap>/spec.md). Requirements use SHALL, and each has at least one `#### Scenario:`
  with **WHEN/THEN** bullets. Commit `docs(openspec): add harden-exec-and-rate-limit change`.
- **T2** Exec parse/metachar/allowlist, no spawn on reject.
- **T3** Exec per-binary argument rules + forced curl flags.
- **T4** Exec sandboxed spawn + rewrite of the shell-dependent `tests/test_exec.py`.
- **T5** `vektor_exec_rejected_total{reason}` + argv0-only logging + description.
- **T6** `agent/rate_limit.py` + `tests/test_rate_limit.py`.
- **T7** Bot wiring: `rate_limiter` param, replies, metric, `num_threads`, composition root.
- **T8** Agent wall-clock budget + metric + `build_agent` env.
- **T9** BASE_PROMPT `## Host safety` paragraph.
- **T10** Red-team `exec-abuse-kill` / `exec-abuse-ps` + `exec-reject` type + offline helper tests.
- **T11** Docs: AGENTS.md, `.env.example`, README.md.
- **T12** Full quality gate, archive, archive check.

Commits (from the approved plan): 1 = T1; 2 = T2–T5 `fix(exec): …`;
3 = T6–T9 `feat(bot): …`; 4 = T10 `test(redteam): …` + T11 docs + T12
`chore(openspec): archive harden-exec-and-rate-limit`.

## Risks and Open Questions

1. **Metachar check on the raw string vs. quoted args.** `curl "https://x/?a=1&b=2"`
   is rejected even though, without a shell, `&` would be harmless. The approved plan
   says to check the raw string. Impact is low because CVE data comes from MCP
   (`skills/cve.md` says never to curl CVE data). Kept as approved and recorded in design.md.
2. **`preexec_fn` + threads.** Python docs warn that `preexec_fn` is unsafe in
   multi-threaded processes (TeleBot worker threads + ThreadPoolExecutor). Mitigation:
   the function only calls `resource.setrlimit` (async-signal-safe in practice),
   with no logging and no locks. An alternative is `process_group=0` plus
   `resource.prlimit` (Linux-only), which is not portable to macOS dev. Kept per the approved plan
   and recorded in design.md.
3. **Platform rlimit differences.** On macOS `RLIMIT_AS` is often unenforced or
   raises on set. On Linux `RLIMIT_NPROC` counts the user's *threads* across
   all processes, so 64 could make curl's threaded resolver fail for a user with
   many processes. D7 uses try/except. If a real curl run fails on CI/Linux, raise NPROC
   or drop it. Tests must not assert that the limits are enforced, only that
   `preexec_fn` is passed and that a function call against a fake `resource` module sets them.
4. **Test interpreter resolution.** `python3` via `_SAFE_PATH` may hit a
   CLT stub on macOS. The fixture must inject `dirname(sys.executable)` into the search path (see Reality Check).
5. **Red-team objective assertion** needs a new case type and helper (not just
   dataset rows), because the live test only sees the final reply. Covered offline.
   The live suite stays unrun.
6. **`/new` is rate limited** (it goes through `handle_message`). Decided:
   acceptable, and the spec says so.
7. **Approved-plan wording `cooldown_s`** is dropped (D10).
8. **Reason label `parse`** is a 4th exec reject reason (Reality Check).
9. **T12 command:** `check_openspec_archived.py` needs piped stdin. A bare
   `python scripts/check_openspec_archived.py` hangs or fails.
10. **Existing spec text** in `cve-tool` mentions "exec-only mode" (MCP
    fallback). With MCP down, the agent can still curl CVE.org over https, so the rules allow
    it (`https://cveawg.mitre.org/...` URLs have no metachars when the query has no `&`). No
    conflict. No existing spec describes ExecTool behavior, so the new `exec-tool`
    capability conflicts with nothing.


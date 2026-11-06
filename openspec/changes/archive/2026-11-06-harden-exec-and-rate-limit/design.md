# Design

## Context

`ExecTool.execute(command)` ran `subprocess.run(command, shell=True)` with the
bot's environment and cwd. Anything the model typed ran with the bot's
privileges: `ps`, `kill`, `cat .env`, `echo $TELEGRAM_BOT_TOKEN`. The only
guard was the system prompt. Separately, TeleBot's 2 default worker threads
and an 8 × 30 s worst-case agent run let a single user monopolize the bot.

## Goals / Non-Goals

Goals:

- Code-level controls on `exec` that hold even if the model fully complies.
- Keep the existing `exec` output contract: failures are returned as strings,
  `ToolError` only for a missing `command`, head+tail truncation, and the
  `exit_code:` line is always preserved.
- Bound per-user load (in-flight + window) and per-run wall-clock time.

Non-Goals:

- No container or seccomp sandbox. rlimits and isolation are best-effort and
  POSIX-only.
- No per-tool rate limiting; the limiter is per user, per message.
- The prompt paragraph is not a control; nothing relies on it.

## Decisions

- **D1 — no shell.** `shlex.split(command)`; a `ValueError` is rejected as
  `"Command rejected: could not parse command."` (reason `parse`). The argv runs
  via `subprocess.Popen(argv, shell=False)`. `parse` is its own reason label:
  folding it into `metachar` would mislead.
- **D2 — metachar check on the raw string**, before parsing: any of
  `| ; & $ \` > < ( )` or a newline → `"Command rejected: shell operators are
  not allowed."`, exit code -1, reason `metachar`.
- **D3 — allowlist.** Default `curl, dig, nslookup, whois, host`;
  `EXEC_ALLOWED_COMMANDS` (comma-separated, trimmed, empty entries dropped;
  empty → default plus a warning) overrides it. `argv[0]` must be a bare name
  (no `/`); it is resolved with `shutil.which(name, path=_SAFE_PATH)` where
  `_SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"`, and the resolved absolute path
  is executed. Unresolvable → exit -1 "not found" string. Reject text:
  `"Command rejected: '<name>' is not an allowed command. Allowed: …"` (reason
  `not_allowed`).
- **D4 — curl option allowlist** (reason `bad_arg`). A denylist was rejected
  in review: `--url-query https://x@/etc/hosts` URL-encoded a local file into
  the query, and unknown short flags inside a cluster swallowed the rest
  (`-E/etc/passwd`). Only these options are accepted:
  `-s -S -I -i -f -v` (`--silent --show-error --head --include --fail
  --verbose --compressed`) and the value options `-H/--header`,
  `-A/--user-agent`, `-X/--request` (GET/HEAD only), `-m/--max-time`,
  `--connect-timeout`, `--retry` (numeric), `-w/--write-out` (no `%output`),
  `--url` (same rules as a positional URL). Everything else is rejected,
  including any `--opt=value` form. Short clusters are allowed only if every
  character is allowlisted, and a value option must be the last character
  (its value is the next argument). `@` anywhere in a `-H/-A/-w` value or in a
  URL is rejected. Exactly one URL (positional or `--url`): it must be
  `https://`, must not contain glob characters `[]{}` after the host, and its
  host must not be `localhost`/`*.localhost` or a literal non-global IP
  (loopback, private, link-local, unspecified, reserved, multicast; checked
  with `ipaddress`, including IPv4-mapped IPv6 and curl's numeric forms like
  `127.1` and `2130706433` via `inet_aton`). IPv4-compatible (`::/96`) and
  NAT64 (`64:ff9b::/96`) and SIIT IPv4-translated (`::ffff:0:0:0/96`)
  literals are rejected outright, since they can embed
  a private IPv4. Any `%` in the URL authority is rejected: curl
  percent-decodes the host (`https://%31%32%37.0.0.1/` reaches 127.0.0.1),
  which would bypass every literal-host check. `-L/--location` is
  deliberately not allowed: a public host could redirect to a loopback or
  private one, and the host check only sees the first URL. Proxy, SOCKS, DoH,
  `--connect-to`, `--resolve` and `--unix-socket` are outside the allowlist.
  Forced appends: `--globoff`, `--max-time <timeout>`,
  `--max-filesize 10485760`, `--proto =https`, `--proto-redir =https`.
- **D5 — dig/nslookup/whois/host rules.** Only `<tool> <name>` plus an
  optional record type (`A AAAA MX TXT NS CNAME SOA PTR ANY`): positional for
  `dig`, `-t TYPE` for `host`, `-type=`/`-query=` for `nslookup`, none for
  `whois`; `dig +short` is also allowed. The name must match
  `[A-Za-z0-9_][A-Za-z0-9_.:-]*`. Every other option (server `@x`, `-p`,
  `-port=`, `-T`, `-vc`, `-f`, `whois -h`) and any second positional (which
  `host`/`nslookup` treat as a server) is rejected.
- **D6 — isolation.** Env is exactly `PATH=_SAFE_PATH`, `LANG` (inherited or
  `C.UTF-8`), `HOME=<tmpdir>` (`build_exec_env(home)`, an allowlist like
  `tools/mcp.py:build_server_env`). cwd is a fresh
  `tempfile.TemporaryDirectory()` per call, removed afterwards.
- **D7 — rlimits and session.** `preexec_fn` (POSIX) sets `RLIMIT_CPU` =
  ceil(timeout), `RLIMIT_AS` = 512 MB, `RLIMIT_FSIZE` = 10 MB,
  `RLIMIT_CORE` = 0; each `setrlimit` is wrapped in try/except and silently
  skipped on failure. `RLIMIT_NPROC` is deliberately not set (see Risks).
  Plus `start_new_session=True`, `stdin=DEVNULL`, piped stdout/stderr.
- **D8 — bounded collection and timeout.** Two reader threads read the pipes
  with a combined byte cap of max(64 KiB, 4 × `max_output_chars`); once it is
  exceeded the process group is SIGKILLed and the result carries
  `[output exceeded N bytes; command killed]`, exit -1. On timeout the group
  is SIGKILLed → `"Command timed out after Ns."`, exit -1. `killpg` ignores
  `ProcessLookupError` and falls back to `proc.kill()` on other errors; joins
  after a kill share a 1 s grace period, and a pipe whose reader thread is
  still blocked (a child that escaped the group with its own `setsid` keeps
  it open) is not closed — closing a `BufferedReader` would block on the
  reader's lock — so the daemon reader ends when that process closes the
  pipe. `execute()` returns within about timeout + 2 s.
  The escaped process's reader thread and pipe fd stay open until it exits;
  only operator-added commands can do this (the default tools do not fork).
- **D9 — logging.** `exec argv0=<name> outcome=allowed|rejected:<reason>|
  timeout|output_cap|error exit=<n>`; arguments are never logged, and a
  non-allowlisted name is logged as `argv0=<other>`. Rejections increment
  `vektor_exec_rejected_total{reason}` with
  `reason ∈ metachar|not_allowed|bad_arg|parse`.
- **D10 — RateLimiter(max_per_window=10, window_s=60.0, clock).**
  `try_acquire(user_id) -> RateDecision(ok, reason)`: in-flight first
  (`in_flight`), then the sliding window of accepted timestamps (`window`).
  Only accepted acquisitions consume a slot; `release` is idempotent; one lock
  guards both. No `cooldown_s` (in-flight + window cover it).
- **D11 — rejection replies.** `_BUSY_REPLY` and `_RATE_LIMITED_REPLY`, exactly
  one reply, no LLM call, history untouched; logged as
  `rate limited user=<id> reason=<r>`. The limiter sits inside the handler's
  `try`, after the auth gate and before the document branch, so uploads and
  `/new` are counted; `/documents` and `/delete` are separate handlers and are
  not.
- **D12 — run budget.** `Agent(max_run_seconds=None, clock=time.monotonic)`;
  before each iteration after the first **and before every tool call**,
  `clock() - start >= budget` trips. Inside a turn, the remaining calls get
  `skipped: run budget exceeded` (one tool result per call id keeps the
  history valid), then `BUDGET_EXCEEDED_REPLY` is appended and returned
  without another LLM call (metric incremented once). `None`/`<= 0` disables
  it; `AGENT_MAX_RUN_SECONDS` defaults to 120, non-numeric/NaN/inf warn and
  fall back. Without the per-call check one reply carrying 20 × `exec` (30 s
  each) would overrun the budget by 10 minutes.
- **D12b — tool calls per reply.** `max_tool_calls_per_turn` (default 4,
  `AGENT_MAX_TOOL_CALLS_PER_TURN`, invalid or < 1 → warn and use 4): calls
  past the cap get `skipped: too many tool calls in one turn (max N); …`
  and the loop continues normally.
- **D13 — worker threads.** `BOT_WORKER_THREADS` default 4; bad or < 1 → warn
  and use 4; above 64 → warn and clamp to 64.
- **D14 — spec layout.** New `exec-tool` capability (ADDED only);
  `message-io-safety` and `agent-loop` get ADDED requirements (no MODIFIED
  blocks are needed).

## Risks / Trade-offs

- **Raw-string metachar check vs. quoted args.** `curl "https://x/?a=1&b=2"` is
  rejected although, without a shell, `&` would be harmless. Accepted: it is the
  simplest rule to audit, and CVE data comes from MCP (`skills/cve.md` forbids
  curling CVE data).
- **`preexec_fn` in a threaded process.** Python warns that `preexec_fn` is
  unsafe with threads (TeleBot workers, retrieval executor). The function only
  calls `resource.setrlimit`, with no logging and no locks. The alternative
  (`process_group=0` + Linux-only `resource.prlimit`) is not portable to macOS.
- **Platform rlimit differences.** macOS often rejects or ignores `RLIMIT_AS`.
  `RLIMIT_NPROC` was dropped: it counts every process (and, on Linux, every
  thread) of the uid, so a cap of 64 makes curl's and dig's resolver threads
  fail on Linux and every fork fail on a busy macOS account. The
  process-group kill and the timeout cover runaway forks. Every remaining
  limit is best-effort and individually guarded; tests assert that the limits
  are requested, not that they are enforced.
- **DNS-resolved private addresses (residual SSRF).** The curl host check
  covers literal addresses and `localhost` only. A public name that resolves
  to a private or loopback address (DNS rebinding, an attacker-controlled
  zone) still reaches it over https. Accepted: https-only, GET/HEAD-only,
  no request bodies, and one URL per call keep the impact low. Pinning
  resolution (e.g. `--resolve` after a vetted lookup) is a possible follow-up.
- **Raw metachar set includes NUL and CR.** NUL would otherwise make `Popen`
  raise; CR is a newline variant.
- **Budget overrun bound.** The budget is checked between iterations and
  between tool calls, and cannot interrupt an in-flight LLM call or tool, so
  the worst case overruns it by one LLM call or one tool call (at most
  `EXEC_TIMEOUT` for `exec`).
- **Validation rejects use a slot.** The limiter runs before input
  validation, so an empty or oversized message still uses a window slot.
  Accepted: it is still a message, and it keeps the gate order simple.
- **`/new` is rate limited.** It goes through `handle_message`; it is cheap
  but still a message. Accepted.

# Proposal

## Why

- `EXEC_TIMEOUT` was parsed in three places (`cve_core`, `tools/mcp.py`,
  `bot.build_tool_registry`) with no range check: `abc` crashed startup in
  `bot.py`, and `nan`/`inf`/`<= 0` were accepted (a `nan` timeout made the CVE
  run deadline `nan`, so every CVE lookup failed).
- A `datePublished` within 5 minutes of `datetime.min` (e.g.
  `0001-01-01T00:00:00Z`) made the window cutoff `at - 5 min` raise
  `OverflowError`, failing the whole `get_latest_cve` call.

## What Changes

- All three call sites use `config.positive_env("EXEC_TIMEOUT", 30.0, float)`:
  invalid, `<= 0`, NaN or inf fall back to 30 with a warning.
- Window membership is tested as `anchor - at > 5 min` (equivalent, but never
  overflows), so extreme timestamps group like any other.

## Impact

- Code: `cve_core.py`, `tools/mcp.py`, `bot.py`, `agent/cve_selector.py`.
- Tests: `tests/test_exec_timeout_env.py`, `tests/test_cve_selector.py`.
- Specs: `exec-tool`, `cve-tool`.
- Docs: AGENTS.md (env table, test coverage).

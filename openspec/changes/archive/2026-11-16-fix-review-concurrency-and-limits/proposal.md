# Proposal

## Why

Review of PR #11 found four defects:

- TeleBot runs `BOT_WORKER_THREADS` (default 4) workers, and the rate limiter
  serializes only per user. Two users in one group chat could run the agent
  concurrently on the same in-memory history list: tool results interleave,
  the trim races, and `/new` can clear the history mid-run.
- The CVE run deadline (80 % of `EXEC_TIMEOUT`) covered only the record
  fetches; the GitHub commit-discovery request still used the full client
  timeout, so a slow GitHub could push the call past the MCP round-trip.
- CVSS ties inside a window compared raw `datePublished` strings, so
  mixed-offset timestamps could pick the older CVE
  (`2026-01-01T12:02:00+02:00` = 10:02Z beat `2026-01-01T10:04:00Z`).
- `AGENT_MAX_ITERATIONS` used a bare `int()`: `eight` crashed startup and `0`
  produced an agent that never called the LLM.

## What Changes

- Agent runs are serialized per chat: a per-chat lock is held for the whole
  run plus the trim and is taken by `/new` and the document-upload turn too.
  Different chats still run in parallel; the lock is released on errors.
- CVE discovery shares the run deadline: its request timeout is capped to the
  time left, and past the deadline it is not sent.
- CVSS ties compare parsed instants (naive = UTC); unparseable timestamps rank
  oldest.
- `AGENT_MAX_ITERATIONS` that is non-integer or < 1 warns and falls back to 8.

## Impact

- Code: `agent/conversation.py`, `cve_core.py`, `agent/cve_selector.py`, `bot.py`.
- Tests: `tests/test_conversation.py`, `tests/test_cve_core.py`,
  `tests/test_cve_selector.py`, `tests/test_agent.py`.
- Specs: `conversation`, `agent-loop`, `cve-tool`.
- Docs: AGENTS.md (env table, Conversation, CVE selection).

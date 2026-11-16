# Proposal

## Why

Review of the uncommitted concurrency/limits work found:

- A failed agent run left the user message (and partial assistant/tool
  messages) in the history and skipped the trim, so repeated failures piled
  up orphan user messages beyond `CONVERSATION_MAX_MESSAGES`.
- The upload turn bypassed an override of `ConversationManager.handle`.
- Chat-busy rejections were counted in the rate-limit metric.
- Same-instant CVSS ties in different offsets fell back to raw-string order.
- `positive_env` crashed on huge ints and had no upper bound;
  `EXEC_TIMEOUT=1e15` broke downstream timeout APIs; `MCP_STARTUP_TIMEOUT`
  used a bare `float()`; an explicit MCP `call_timeout` of `0.0` was silently
  replaced and a negative one accepted.
- A malformed GitHub commit entry raised out of the never-raises CVE lookup.

## What Changes

- A failed run rolls the chat history back to its pre-turn state; the trim
  runs after every run. `handle(chat_id, text, *, reservation=None)` is the
  single public turn entry point (upload turn included).
- New `vektor_chat_busy_total{path=text|upload|new}`; `chat_in_flight` leaves
  `vektor_rate_limited_total`.
- Equal-instant ties break on the parsed CVE id `(year, number)`.
- `positive_env` treats overflow as invalid and takes an optional `maximum`
  (clamp with a warning, like `BOT_WORKER_THREADS`); `EXEC_TIMEOUT` and
  `MCP_STARTUP_TIMEOUT` are capped at 3600 s; `MCP_STARTUP_TIMEOUT` uses it.
  An explicit non-positive/non-finite MCP `call_timeout` raises `ValueError`.
- Malformed commit entries are skipped; a non-empty payload with none
  well-formed returns the "Failed to discover" message.

## Impact

- Code: `agent/conversation.py`, `bot.py`, `metrics.py`, `config.py`,
  `agent/cve_selector.py`, `cve_core.py`, `tools/mcp.py`.
- Tests: `test_conversation.py`, `test_bot_rate_limit.py`, `test_config.py`,
  `test_exec_timeout_env.py`, `test_cve_selector.py`, `test_cve_core.py`.
- Specs: `conversation`, `message-io-safety`, `cve-tool`, `exec-tool`.
- Docs: AGENTS.md.

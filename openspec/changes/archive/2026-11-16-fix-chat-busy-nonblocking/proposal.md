# Proposal

## Why

Review of the per-chat serialization fix found that it trades one problem for
another:

- The per-chat lock is taken with a blocking `with`, so a second message for a
  busy chat parks a TeleBot worker (`BOT_WORKER_THREADS`, default 4) for up to
  `AGENT_MAX_RUN_SECONDS`. One busy group chat with 4+ senders stalls every
  other chat.
- `/new` waits behind an in-flight run in the same way.
- The lock table gains a `threading.Lock` per chat id forever.
- `Agent(max_iterations=0)` (or negative) still builds an agent that never
  calls the LLM when constructed directly.
- Positive-int env parsing is implemented three times (`_positive_env`,
  `_max_tool_calls_per_turn_from_env`, `CONVERSATION_MAX_MESSAGES`).

## What Changes

- `ConversationManager.handle` never blocks: a set of in-flight chat ids behind
  one small lock replaces the lock table. A message (incl. `/new` and the
  upload turn) for a chat with a run in flight raises `ChatBusyError`
  immediately — no LLM call, history untouched. The bot sends exactly one busy
  reply (the upload turn says the document is already ingested) and counts
  `vektor_rate_limited_total{reason="chat_in_flight"}`.
- `Agent.__init__` raises `ValueError` for `max_iterations < 1`.
- Env knobs share `config.positive_env()` (same defaults and fallbacks).

## Impact

- Code: `agent/conversation.py`, `agent/agent.py`, `bot.py`, `config.py`,
  `metrics.py` (help text).
- Tests: `tests/test_conversation.py`, `tests/test_bot_rate_limit.py`,
  `tests/test_agent.py`, `tests/test_config.py`.
- Specs: `conversation`, `agent-loop`, `message-io-safety`.
- Docs: AGENTS.md (Conversation, Message I/O, Observability, test coverage).

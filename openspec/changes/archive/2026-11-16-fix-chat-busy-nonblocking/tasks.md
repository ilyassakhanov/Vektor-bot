# Tasks

## 1. Non-blocking per-chat exclusion

- [x] 1.1 Threaded tests: second run and `/new` in a busy chat raise `ChatBusyError` without blocking, history untouched and not cleared, other chats proceed, busy mark cleared after an exception, in-flight set empty after runs — `python -m pytest tests/test_conversation.py`
- [x] 1.2 `ConversationManager`: in-flight chat-id set behind one small lock replaces the per-chat lock table; `ChatBusyError`
- [x] 1.3 Handler tests: text and `/new` get one `_BUSY_REPLY`, upload turn ingests then gets one `_DOCUMENT_BUSY_REPLY`, no LLM call, `chat_in_flight` metric — `python -m pytest tests/test_bot_rate_limit.py`
- [x] 1.4 `bot.py` maps `ChatBusyError` on the text path and the upload turn

## 2. Agent iteration guard

- [x] 2.1 Tests: `Agent(max_iterations=0/-1)` raises `ValueError`, `1` works — `python -m pytest tests/test_agent.py`
- [x] 2.2 `Agent.__init__` validates `max_iterations`

## 3. Shared env parsing

- [x] 3.1 Tests for `config.positive_env` (unset, valid int/float, invalid/zero/negative/NaN/inf warn and fall back) — `python -m pytest tests/test_config.py`
- [x] 3.2 `bot.py` and `agent/conversation.py` use `config.positive_env`; duplicate parsers removed

## 4. Docs, quality gates and archive

- [x] 4.1 AGENTS.md Conversation, Message I/O, Observability and test coverage
- [x] 4.2 `ruff check .`, `ruff format --check .`, `mypy .` pass
- [x] 4.3 `python -m pytest` passes offline
- [x] 4.4 `npx --yes @fission-ai/openspec@1.14.1 validate --all --strict` passes
- [x] 4.5 Archive this change in the same pull request that closes its last task

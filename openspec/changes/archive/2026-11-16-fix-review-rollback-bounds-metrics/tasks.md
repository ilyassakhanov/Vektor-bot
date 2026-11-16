# Tasks

## 1. History rollback and single turn entry point

- [x] 1.1 Tests: N consecutive LLM errors leave history unchanged, partial tool messages rolled back, unexpected errors roll back, a later run sees no orphan user messages — `python -m pytest tests/test_conversation.py`
- [x] 1.2 `_handle_reserved` deletes everything the failed run appended and trims in `finally`
- [x] 1.3 Tests: `handle(..., reservation=...)` runs inside the held mark, mismatched/stale reservations rejected, an overridden `handle` sees the upload turn — `python -m pytest tests/test_conversation.py tests/test_bot_rate_limit.py`
- [x] 1.4 `ConversationManager.handle` takes `reservation`; `ChatReservation.handle` and the upload turn route through it

## 2. Chat-busy metric

- [x] 2.1 `vektor_chat_busy_total{path}` in `metrics.py`; `bot.py` counts text/upload/new; `chat_in_flight` removed from the rate-limit metric; tests updated

## 3. CVE tie-break and discovery

- [x] 3.1 Tests: same instant in different offsets breaks on the numeric CVE id, unparseable ids rank lowest — `python -m pytest tests/test_cve_selector.py`
- [x] 3.2 Tie key uses the parsed `(year, number)` CVE id
- [x] 3.3 Tests: partly-malformed commits skip bad entries; fully-malformed payload returns the discovery-failed message — `python -m pytest tests/test_cve_core.py`
- [x] 3.4 `_discover_cve_ids` validates entry shapes

## 4. Env bounds

- [x] 4.1 Tests: huge values never crash, clamp above the maximum, `MCP_STARTUP_TIMEOUT` fallback/clamp, `main()` passes the parsed value, explicit MCP `call_timeout` validation — `python -m pytest tests/test_config.py tests/test_exec_timeout_env.py`
- [x] 4.2 `positive_env` catches `OverflowError` and takes `maximum`; `EXEC_TIMEOUT`/`MCP_STARTUP_TIMEOUT` capped at `config.MAX_TIMEOUT_S`; `McpStdioClient` validates an explicit `call_timeout`

## 5. Docs

- [x] 5.1 AGENTS.md: env rows, Conversation (incl. upload reservation trade-off), Observability, CVE selection, test coverage

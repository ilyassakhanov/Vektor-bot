# Tasks

## 1. Chat reservation

- [x] 1.1 Tests: `reserve()` raises at once on a busy chat, blocks only its own chat, keeps the turn's history, releases on exception, handles `/new`, unusable after exit — `python -m pytest tests/test_conversation.py`
- [x] 1.2 `ConversationManager.reserve()` + `ChatReservation.handle()`; `handle()` built on them

## 2. Upload reserves the chat before ingestion

- [x] 2.1 Handler tests: upload into a busy chat → one `_DOCUMENT_BUSY_REPLY`, no get_file/download, store empty, no LLM call, metric, slot released; normal upload full sequence with the notice in history; reservation across ingest rejects a same-chat text message but not other chats; reservation released after download/extract/ingest/agent errors — `python -m pytest tests/test_bot_rate_limit.py`
- [x] 2.2 `build_document_handler` reserves the chat before any download and runs the agent turn inside the reservation; `_DOCUMENT_BUSY_REPLY` says the document was not ingested

## 3. Distinct chat-busy reply

- [x] 3.1 Tests: text and `/new` in a busy chat get `_CHAT_BUSY_REPLY`; per-user in-flight keeps `_BUSY_REPLY`
- [x] 3.2 `_CHAT_BUSY_REPLY` on the text path

## 4. Docs, quality gates and archive

- [x] 4.1 AGENTS.md Conversation, Documents, Rate limiting and test coverage
- [x] 4.2 `ruff check .`, `ruff format --check .`, `mypy .` pass
- [x] 4.3 `python -m pytest` passes offline
- [x] 4.4 `npx --yes @fission-ai/openspec@1.14.1 validate --all --strict` passes
- [x] 4.5 Archive this change in the same pull request that closes its last task

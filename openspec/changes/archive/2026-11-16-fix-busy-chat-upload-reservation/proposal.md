# Proposal

## Why

Review of the non-blocking busy-chat fix found two gaps:

- An upload into a busy chat is wasted. The chat-busy check happens only in
  `conv.handle`, after download, extraction, embedding and the
  "✅ Document ready" reply. The user gets contradictory replies, and the
  upload turn never enters history, so follow-ups lack the upload context.
- In a group chat a `ChatBusyError` is usually caused by ANOTHER member's run
  (the per-user limiter already catches the same user), but the text path
  replies "I'm still working on your previous message".

## What Changes

- `ConversationManager.reserve(chat_id)`: a context manager that takes the
  chat's busy mark up front (raises `ChatBusyError` immediately), holds it for
  the block and releases it in `finally`; `ChatReservation.handle(text)` runs a
  turn inside it. `handle()` is `reserve` + one turn.
- The document handler reserves the chat before any download and holds it
  across ingestion and the agent turn. A busy chat gets exactly one
  `_DOCUMENT_BUSY_REPLY` (the document was NOT ingested, resend it) — no
  download, no ingest, no LLM call; `chat_in_flight` is counted.
- New `_CHAT_BUSY_REPLY` for a busy chat on the text path and `/new`;
  `_BUSY_REPLY` stays for the per-user limiter's in-flight case.

## Impact

- Code: `agent/conversation.py`, `bot.py`.
- Tests: `tests/test_conversation.py`, `tests/test_bot_rate_limit.py`.
- Specs: `conversation`, `documents`.
- Docs: AGENTS.md (Conversation, Documents, Rate limiting, test coverage).

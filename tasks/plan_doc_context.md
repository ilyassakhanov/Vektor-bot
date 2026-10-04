# Implementation Plan: Document Upload Always Enters Conversation Context (Option A)

## Problem

When a user uploads a document (.txt/.pdf/.docx), `build_document_handler`'s
`handle_document` (`bot.py:281-335`) sends the ingest confirmation via `reply_to`
and only calls `conv.handle(...)` when a caption is present (`bot.py:320-322`:
`if not caption: return`). With no caption the conversation history stays empty —
the model never learns a document was uploaded, so follow-up questions ("what
does that document say about X?") have zero context. Even with a caption, only
the caption text enters history; the upload event itself is never represented,
so the agent sees a bare question with no hint that content was just ingested.

## Prerequisite scope riding along (already implemented, uncommitted)

The working tree already contains an uncommitted prior fix on this same branch
(`bot.py` + `tests/test_bot_documents.py`): `content_types=["text", "document"]`
added to the `@bot.message_handler` in `create_bot` (`bot.py:411`) plus the
regression test `test_create_bot_registers_document_content_type`
(`tests/test_bot_documents.py:483-493`). Without it TeleBot silently drops
document uploads before `handle_message` runs. This fix is prerequisite scope
for the same PR — it is already implemented and verified (ruff/mypy/678 tests
green); the publisher must stage it together with this slice's changes.

**Stray artifacts:** untracked `benchmarks/retrieval-bench-output.txt` and
`results/HYBRID_SEARCH_REPORT.md` are benchmark output and must NOT be staged
or committed by the eventual publisher.

## Design Decision (user-approved, fixed — Option A: always run the agent on upload)

After a successful ingest in `handle_document`:

1. Send the ingest confirmation reply (unchanged, first reply).
2. Build a **synthetic user prompt**: a notice that a document was uploaded,
   e.g. `[document uploaded: "{file_name}", {N} chunks ingested; content is
   now searchable via kb_search]` — using the existing `_chunk_count` helper
   (`bot.py:338-341`). Exact wording is flexible, but the notice MUST include
   the file name and the fact that content is searchable via `kb_search`.
3. If a caption is present, append it to the notice (notice + `"\n\n"` +
   caption) so the agent answers it in the same turn.
4. **ALWAYS** call `conv.handle(message.chat.id, prompt)` and send the agent's
   response as the second reply. No-caption uploads now also produce exactly
   two replies (confirmation + agent reply/ack).
5. Error handling unchanged: `LLMError` / generic exception during the agent
   turn → exactly one friendly `_LLM_ERROR_REPLY` after the confirmation.

**Why Option A:** it is the minimal change that fixes both halves of the
problem at once — every upload lands in conversation context (so follow-ups
work), and captions get answered in the same turn without extra branching.
Alternatives were rejected: a synthetic `assistant` history entry without an
agent run would desync the message roles the agent/trim logic expects
(leading messages must be `user`-role, see `ConversationManager._trim`), and
holding uploads out of history would leave the follow-up-context bug in place.

## Architecture / Flow

```
Telegram document ──→ bot.get_file + bot.download_file
                  → documents.extract_text
                  → KbIngestTool.execute (fresh wiring, BEFORE the agent)
                  → reply #1: ingest confirmation (unchanged)
                  → prompt = upload notice (file name + N chunks + kb_search hint)
                     [+ "\n\n" + caption when present]
                  → conv.handle(chat_id, prompt)  ← ALWAYS now
                  → reply #2: agent response (or _LLM_ERROR_REPLY on LLMError/
                    generic exception)
```

- `ConversationManager.handle` appends the synthetic user message to history,
  runs the agent, trims — identical to a normal text turn. No new code in
  `agent/conversation.py`.
- The upload notice reaching the LLM is bounded (file name + counts only —
  never extracted content), consistent with the repo rule that extraction
  output never crosses the LLM tool boundary.
- Auth gate, KB-disabled gate, document-wins-over-text, download/extract/
  ingest error paths, and log redaction are all untouched — every error path
  fails BEFORE the agent turn and still yields exactly one friendly reply.
- `create_bot` needs no change for this slice (the `content_types` fix is
  already in the tree).

## Task List

### Phase 1: Handler change (TDD) + test updates

- [ ] Task 1: `bot.py` — `handle_document` builds the upload notice, appends the
  caption, and ALWAYS calls `conv.handle`; `tests/test_bot_documents.py` —
  update the 5 affected tests, verify the 2 caption-error tests, add the new
  context-propagation regression test (test-first)

### Checkpoint: Core

- [ ] `python -m pytest tests/test_bot_documents.py` green
- [ ] Full `python -m pytest` green (no regressions)
- [ ] `ruff check .` + `ruff format --check .` + `mypy .` green

### Phase 2: Docs

- [ ] Task 2: Docs — `skills/documents.md` wording (agent runs on upload with a
  notice) + `AGENTS.md` Documents section ("Two-reply caption UX" → every
  successful upload gets two replies; upload lands in conversation context)

### Checkpoint: Complete

- [ ] All acceptance criteria met (see Acceptance mapping in todo)
- [ ] `git status` shows only intended files; stray artifacts unstaged
- [ ] Ready for review

## Required Test Updates (tests/test_bot_documents.py — exact mapping)

| Test | Change |
|---|---|
| `test_document_downloaded_extracted_ingested_confirmed` | `ScriptedLLM` gets one canned response; assert 2 replies and `len(llm.chat_calls) == 1` |
| `test_no_caption_exactly_one_reply` | Rename/rewrite → `test_no_caption_gets_confirmation_then_agent_ack`: two replies (confirmation prefix, then the scripted agent answer) |
| `test_ingested_document_findable_via_kb_search` | `ScriptedLLM([])` would exhaust → give it one canned response; kb_search assertions unaffected |
| `test_caption_routed_to_agent_two_replies_in_order` | Still 2 replies; last user message now ends with the caption AND contains the upload notice (replace `messages[-1].content == caption`) |
| `test_document_wins_over_text` | Now 2 replies and `len(llm.chat_calls) == 1`; plain `text` attribute still must NOT be sent as agent input |
| Error paths (unknown ext, corrupt pdf, get_file API error, download API error, embedder failure, missing file_path, nameless doc) | **Unchanged** — fail BEFORE the agent turn, stay exactly 1 reply |
| `test_caption_llm_error_confirmation_then_friendly_reply` | Structurally unchanged (2 replies, second is error reply) — verify |
| `test_caption_non_llm_error_still_gets_friendly_reply` | Structurally unchanged — verify |
| **NEW** `test_document_upload_propagates_context_to_next_message` | After a no-caption upload (2 replies), a follow-up `conv.handle(42, "...")` must show the upload turn in the LLM's message history — `ScriptedLLM` with 2 responses; assert some user message contains the file name |

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| `ScriptedLLM([])` tests that now exhaust their script | Low | Every test whose flow reaches the agent gets exactly one canned response (explicitly listed above). |
| Synthetic prompt phrasing varies per upload | Low | Tests assert only on stable invariants: file name present, `kb_search` mentioned, caption appended at the end — not exact wording. |
| Caption-error tests drift when the handler changes | Low | Error paths fail before the agent turn; the two caption-error tests are assert-only verifications, no rewrite. |
| Extra LLM call per no-caption upload (cost/latency) | Accepted | User-approved trade-off; local Ollama costs $0 and the notice prompt is tiny. |
| Publisher stages stray artifacts | Med | Called out in Task 2 / todo Checkpoint: stage only `bot.py`, `tests/test_bot_documents.py`, `skills/documents.md`, `AGENTS.md`. |

## Open Questions

None — design fixed and user-approved (Option A: always run the agent on
upload; notice wording flexible within the stated musts; error behavior
preserved).

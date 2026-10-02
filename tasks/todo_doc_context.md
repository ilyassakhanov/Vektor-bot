# Todo: Document Upload Always Enters Conversation Context (Option A)

Checklist companion to `tasks/plan_doc_context.md`. Work top-down; do not skip checkpoints.

Prerequisite riding along (already implemented + verified, do NOT redo): uncommitted
`content_types=["text", "document"]` in `create_bot` (`bot.py`) +
`test_create_bot_registers_document_content_type` (`tests/test_bot_documents.py`).
Stray artifacts `benchmarks/retrieval-bench-output.txt` and `results/HYBRID_SEARCH_REPORT.md`
must NOT be staged/committed.

## Phase 1: Handler change (TDD) + test updates

- [x] Task 1: `bot.py` — in `handle_document`, after the confirmation reply build the
  upload notice (`[document uploaded: "{file_name}", {N} chunks ingested; content is
  now searchable via kb_search]` via `_chunk_count`), append `"\n\n" + caption` when a
  caption is present, and ALWAYS `conv.handle(message.chat.id, prompt)` → second reply;
  keep `LLMError`/generic-exception → one friendly `_LLM_ERROR_REPLY` after the
  confirmation (+ `tests/test_bot_documents.py`, test-first)

Test updates (see plan table for the full 1:1 mapping):

- [x] `test_document_downloaded_extracted_ingested_confirmed`: ScriptedLLM canned
  response; assert 2 replies, `len(llm.chat_calls) == 1`
- [x] `test_no_caption_exactly_one_reply` → rename/rewrite: two replies
  (confirmation prefix, then scripted agent answer)
- [x] `test_ingested_document_findable_via_kb_search`: give ScriptedLLM one canned
  response; kb_search assertions unaffected
- [x] `test_caption_routed_to_agent_two_replies_in_order`: still 2 replies; last user
  message ends with the caption AND contains the upload notice
- [x] `test_document_wins_over_text`: 2 replies, `len(llm.chat_calls) == 1`; plain text
  still NOT sent as agent input
- [x] Error paths (unknown ext, corrupt pdf, get_file/download API error, embedder
  failure, missing file_path, nameless doc): unchanged — 1 reply, `llm.chat_calls == []`
- [x] `test_caption_llm_error_…` + `test_caption_non_llm_error_…`: verified structurally
  unchanged (2 replies, second is `_LLM_ERROR_REPLY`)
- [x] NEW `test_document_upload_propagates_context_to_next_message`: no-caption upload
  (2 replies) → follow-up `conv.handle(42, "...")` → ScriptedLLM with 2 responses; some
  user message in history contains the file name

### Checkpoint: Core

- [x] `python -m pytest tests/test_bot_documents.py` green
- [x] Full `python -m pytest` green
- [x] `ruff check .` + `ruff format --check .` + `mypy .` green

## Phase 2: Docs

- [x] Task 2: Docs — `skills/documents.md`: minor wording (agent now runs on upload with
  a notice; keep existing `kb_search` rules) + `AGENTS.md`: Documents section /
  "Two-reply caption UX" → EVERY successful upload gets confirmation + agent reply and
  the upload lands in conversation context (update architecture diagram arrow +
  Testing/Test-coverage bullets if wording is stale)

### Checkpoint: Complete

- [x] All acceptance criteria met (see Acceptance mapping)
- [x] `git status` shows only intended files; stray artifacts unstaged
- [x] Ready for review

## Acceptance (1:1 with spec)

- [x] Ingest confirmation reply sent unchanged as reply #1 after successful ingest
- [x] Synthetic notice includes the file name AND the fact content is searchable via
  `kb_search` (chunk count from `_chunk_count`)
- [x] Caption present → notice + `"\n\n" + caption` as the single agent prompt
- [x] No caption → STILL two replies: confirmation + agent response (exact-two invariant)
- [x] Upload always enters conversation context — follow-up questions see the upload
  turn (proven by the new regression test)
- [x] `LLMError`/generic exception on the agent turn → confirmation + exactly one
  friendly `_LLM_ERROR_REPLY` (never a crash into polling)
- [x] Error paths before the agent turn (download/extract/ingest) unchanged: exactly one
  `_DOC_ERROR_REPLY`, no LLM call
- [x] Auth gate, KB-disabled gate, document-wins-over-text all preserved
- [x] No file content or caption ever logged (file name/outcome only)
- [x] Docs updated: `skills/documents.md` + `AGENTS.md` reflect always-agent-on-upload
- [x] Prerequisite `content_types` fix and stray artifacts handled per PR scope above
- [x] `ruff check .` + `ruff format --check .` + `mypy .` + full `python -m pytest` green

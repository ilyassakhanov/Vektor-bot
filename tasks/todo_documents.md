# Todo: Telegram Document Uploads → RAG Knowledge Base (.pdf, .txt, .docx)

Checklist companion to `tasks/plan_documents.md`. Work top-down; do not skip checkpoints.

## Phase 1: Foundation (pure extraction, no network/Telegram)

- [x] Task 1: `documents.py` — `DocumentError`, `SUPPORTED_EXTENSIONS`, pure `extract_text(content, filename)` dispatching `.txt`/`.pdf`/`.docx` (pypdf + python-docx); add `pypdf>=4.0`, `python-docx>=1.1` to requirements (+ `tests/test_documents.py`, test-first, fixtures generated in-test: minimal handcrafted PDF bytes with a known `Tj` operator, python-docx-generated DOCX, plain TXT)

### Checkpoint: Foundation
- [x] `python -m pytest tests/test_documents.py` green
- [x] Full `python -m pytest` green
- [x] `ruff check .` + `ruff format --check .` + `mypy .` green

## Phase 2: Bot integration

- [x] Task 2: `bot.py` — `build_document_handler(bot, kb, conv)` closure (download → `extract_text` → fresh `KbIngestTool` with `build_tool_registry` wiring → confirmation reply; caption → `conv.handle(chat_id, caption)` → second reply; catch `ApiTelegramException`/`DocumentError`/`ToolError`/`LLMError` → friendly reply); `handle_message(..., document_handler=None)` with `getattr(message, "document", None)`, auth FIRST, KB-off → "Document uploads are not enabled."; `create_bot(conv, allowed_usernames=None, kb=None)`; `main()` passes `kb` (+ `tests/test_bot_documents.py`: fake bot with canned bytes, real tmp-SQLite `KbStack` + local `FakeEmbedder`, SimpleNamespace messages, `ScriptedLLM` caption path)

### Checkpoint: Core
- [x] Full suite + lint/mypy green
- [x] Document flow proven: ingest confirmation reply; caption → agent answer (2 replies); no caption → 1 reply
- [x] KB-disabled document message → "Document uploads are not enabled.", no download
- [x] Auth gate proven for documents (unauthorized → denial, download never attempted)
- [x] Text-message regression proven (SimpleNamespace fixtures without `document` unchanged)
- [x] Error paths proven: unknown ext, corrupt bytes, API error, ToolError, caption LLMError — each one friendly reply, never a crash

## Phase 3: Polish

- [x] Task 3: Docs — `AGENTS.md` (structure table row, architecture diagram document branch, Documents subsection, testing bullet), `.env.example` (NO new keys; optional one-line KB comment), `skills/documents.md` (auto-ingest note + "use kb_search for questions about uploaded docs")

### Checkpoint: Complete
- [x] All acceptance criteria met (see Acceptance mapping)
- [x] Ready for review

## Acceptance (1:1 with spec)

- [x] `.txt` extracted via UTF-8 decode with replacement; `.pdf` via pypdf pages; `.docx` via python-docx paragraphs
- [x] Unknown extension or corrupt file → `DocumentError` → friendly reply, never a crash
- [x] Document message ingested via `KbIngestTool` BEFORE the agent; title = file name (fallback `"document"`)
- [x] Ingestion actually searchable: kb over the same stack finds the ingested content
- [x] Caption → confirmation reply + agent reply (caption routed via ConversationManager); no caption → confirmation only
- [x] KB disabled (`kb=None` / `KB_ENABLED=0`) → "Document uploads are not enabled." reply
- [x] Auth check stays FIRST — unauthorized users' documents are never downloaded
- [x] Text-message path byte-identical for messages without a document
- [x] No new env vars; 20 MB Telegram download cap is the size limit
- [x] `documents.py` has no Telegram deps; tests fully offline (no network/Ollama, no binary fixtures committed)
- [x] pypdf + python-docx in requirements.txt; `pip install -r requirements.txt` sufficient
- [x] `ruff check .` + `ruff format --check .` + `mypy .` + full `python -m pytest` green
- [x] Docs updated; no unrelated architecture changes

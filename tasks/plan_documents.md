# Implementation Plan: Telegram Document Uploads → RAG Knowledge Base (.pdf, .txt, .docx)

## Overview

Let users send documents (.txt, .pdf, .docx) to the bot and have them ingested into the
existing RAG knowledge base, then ask questions about them. The bot (not the agent)
downloads the file, extracts text, and ingests via `KbIngestTool` **before** the agent
loop — a 50-page PDF cannot be echoed as a `kb_ingest` tool argument. If the document
carries a caption, the caption is routed through `ConversationManager` to the agent so
it can immediately `kb_search` and answer; the user gets two replies (ingest
confirmation, then the agent's answer). With no caption, only the ingest confirmation
is sent. Today, document messages hit `handle_message` with `message.text = None` and
arrive at the agent as empty strings — this plan adds the missing document branch.

## Reality Check (repo fit)

`handle_message` (`bot.py:285`) passes `message.text or ""` to the agent; there is no
file download, no text extraction, no document branch. The kb stack
(`build_kb_stack` → `KbStack`) already exposes everything needed for bot-level
ingestion: `store`, `embedder`, `vector_index`, `metadata`, `cfg.kb_chunk_size`,
`cfg.kb_chunk_overlap` — the exact wiring `build_tool_registry` uses for
`KbIngestTool` (`bot.py:170-179`). TeleBot provides `bot.get_file(file_id)` →
`File.file_path` and `bot.download_file(file_path) → bytes`
(`telebot.apihelper.ApiTelegramException` on failure). Existing tests build messages
as `SimpleNamespace` **without a `document` attribute** → the document check must use
`getattr(message, "document", None)`. Vektor is fully synchronous; download + extract +
ingest happen inline in the handler. `tests/conftest.py` sets `KB_ENABLED=0` by
default; kb-on tests build real `KbStack`s over tmp SQLite with fakes (see
`tests/test_kb_tools.py::_make_kb`).

## Architecture Decisions (user-approved, fixed)

1. **Bot-level ingestion, NOT agent-level.** The document handler downloads → extracts
   → ingests via `KbIngestTool` before the agent runs; a caption (if any) is routed to
   the agent via `ConversationManager` so it can `kb_search` immediately.
   Rationale: extraction output (potentially megabytes) must never cross the LLM tool
   boundary as an argument.
2. **Libraries:** `pypdf` for PDF, `python-docx` for DOCX (both pure-Python, both ship
   `py.typed` → mypy-clean). TXT = stdlib `bytes.decode("utf-8", errors="replace")`.
   Added to `requirements.txt` with the repo's `>=` minimum-pin style.
3. **No `KB_MAX_FILE_SIZE` env var.** Telegram already caps bot downloads at 20 MB —
   that is the enforced limit.
4. **Caption routing UX:** ingest → reply with ingest confirmation → if caption
   non-empty, ALSO route `conv.handle(chat_id, caption)` and reply with the agent's
   response (two replies total). No caption → only the ingest confirmation.
5. **KB disabled** (`kb=None` / `KB_ENABLED=0`) → document messages get a
   "Document uploads are not enabled."-style reply. Never crash.
6. **`documents.py` is Telegram-free.** Pure `extract_text(content: bytes,
   filename: str) -> str` dispatching on the lowercased extension, mirroring
   `config.py`/`cve_core.py` module style — fully offline-testable.
7. **Auth stays FIRST.** `handle_message`'s existing auth check runs before any
   document branch; unauthorized users' documents are never downloaded.
8. **Text path unchanged.** `handle_message` gains an optional `document_handler`
   keyword param appended LAST (existing positional call sites in tests keep working).

## Pipeline

```
Telegram document ──→ bot.get_file(file_id) + bot.download_file(file_path)
                                                                    │
                                                                    ▼
                                          documents.extract_text(content, file_name)
                                                                    │
                                                                    ▼
                                          KbIngestTool.execute(text=..., title=file_name)
                                                    │
                                    ┌───────────────┴────────────────┐
                                    ▼                                 ▼
                          no caption → reply                 caption → conv.handle(chat_id, caption)
                          "Ingested N chunks …"              → agent (can kb_search) → reply
```

Error paths (each → one friendly reply, never a crash): download/API error,
`DocumentError` (unknown extension, corrupt file), `ToolError` (embedding service
down), `LLMError` on the caption path.

## Task List

### Phase 1: Foundation (pure extraction, no network/Telegram)

- [ ] Task 1: `documents.py` — `extract_text()` dispatcher + requirements + `tests/test_documents.py` (test-first)

### Checkpoint: Foundation

- [ ] `python -m pytest tests/test_documents.py` green
- [ ] Full `python -m pytest` green (no regressions)
- [ ] `ruff check .` + `ruff format --check .` + `mypy .` green

### Phase 2: Bot integration

- [ ] Task 2: `bot.py` — `build_document_handler` + `handle_message`/`create_bot`/`main` wiring + `tests/test_bot_documents.py`

### Checkpoint: Core

- [ ] Full suite + lint/mypy green
- [ ] Auth gate proven for documents (download never attempted for unauthorized users)
- [ ] Text-message regression proven (SimpleNamespace messages without `document` unchanged)

### Phase 3: Polish

- [ ] Task 3: Docs — `AGENTS.md`, `.env.example`, `skills/documents.md`

### Checkpoint: Complete

- [ ] All acceptance criteria met (see Acceptance mapping in todo)
- [ ] Ready for review

## Configuration

**No new environment variables.** Existing knobs unchanged; document ingestion rides
the kb stack (`KB_ENABLED`, `KB_DB_PATH`, `KB_CHUNK_SIZE`, `KB_CHUNK_OVERLAP`, …).
File size is bounded by Telegram's 20 MB bot-download limit (no `KB_MAX_FILE_SIZE` —
user decision). `.env.example` gets no new keys (at most a comment, per Task 3).

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| mypy complains about `docx`/`pypdf` stubs | Low | Both libs ship `py.typed` (python-docx ≥1.1, pypdf ≥4). If a stray untyped import slips through, add a targeted `# type: ignore[import-untyped]` or mypy override — never broad ignores. |
| Scanned/image PDFs extract to empty text | Low | `extract_text` joins only non-empty page texts; empty result flows into `KbIngestTool`'s existing "Ingested 0 chunks (doc …, no content)" reply. Documented behavior, not an error. |
| 20 MB download blocks the polling loop (sync) | Med | Accepted for a single-user bot; Telegram's cap bounds it. Documented in risks; no async rework. |
| Huge extracted text → large embed batch | Med | `KbIngestTool` already embeds all chunks in one batch; 20 MB cap bounds worst case. Chunking knobs (`KB_CHUNK_SIZE`/`KB_CHUNK_OVERLAP`) already exist. |
| Message with both `document` and `text` | Low | Document branch wins (document present → delegate). Explicit, tested behavior. |
| `telebot` download raises `ApiTelegramException` | Med | Caught in the handler closure → user-friendly reply + warning log; never crashes polling. |
| Sensitive file content leaking to logs | Med | Log only `file_name`/size/outcome — never extracted text or caption content (repo redaction rule). |

## Open Questions

None — all design decisions fixed and user-approved (bot-level ingestion, pypdf +
python-docx, no size env var, two-reply caption UX, disabled-KB message).

## Detailed Task Specs

### Task 1: Document extraction module (`documents.py`)

**Description:** New `documents.py` at the project root (mirroring `config.py` /
`cve_core.py` style — module docstring, `from __future__ import annotations`, module
logger `log = logging.getLogger("vektor.documents")`, type hints throughout, no
Telegram deps). Contents:

- `class DocumentError(Exception)` — raised for unsupported extensions and any
  extraction failure.
- `SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".txt", ".pdf", ".docx"})`.
- `extract_text(content: bytes, filename: str) -> str` — dispatches on
  `Path(filename).suffix.lower()` (or `os.path.splitext`; no filesystem access —
  string ops only):
  - `.txt` → `content.decode("utf-8", errors="replace")`.
  - `.pdf` → `pypdf.PdfReader(io.BytesIO(content))`; join
    `page.extract_text()` across pages, skipping empty/None results.
  - `.docx` → `docx.Document(io.BytesIO(content))` (python-docx); join
    `paragraph.text` across paragraphs, skipping empty ones.
  - Unknown/missing extension → `DocumentError` naming the extension.
  - Any exception inside a branch (corrupt bytes, malformed container) is wrapped in
    `DocumentError` with `exc_info` logged at warning level; the original message is
    preserved via `from exc`.
- No size checks, no network, no env reads.

Write `tests/test_documents.py` FIRST (test-first), generating tiny fixtures **inside
the tests** so no binary fixtures are committed:
- PDF via `pypdf.PdfWriter` + `pypdf` `PdfWriter.add_blank_page` /
  low-level text: simplest reliable approach — build a one-page PDF whose text is
  extractable (e.g. write with `reportlab`-free method: use `pypdf`'s
  `PdfWriter` clone of a minimal handcrafted page object, or assert only on
  ".pdf returns a str without raising" for writer-generated files and use a
  handcrafted minimal PDF byte string with a known `Tj` text operator for the
  content assertion). The test author should prefer the handcrafted minimal-PDF
  constant (a ~20-line byte string) for deterministic extracted-text assertions.
- DOCX via `python-docx`: `Document(); doc.add_paragraph("..."); doc.save(BytesIO)`.
- TXT via plain `bytes`.

Add to `requirements.txt`: `pypdf>=4.0` and `python-docx>=1.1` (alphabetical/logical
placement matching the file's current flat list; then `pip install -r requirements.txt`).

**Acceptance criteria:**
- [ ] `.txt` decodes with replacement (invalid UTF-8 never raises)
- [ ] `.pdf` returns extracted text from a known-content PDF
- [ ] `.docx` returns paragraph text from a python-docx-generated file
- [ ] Extension matching is case-insensitive (`.PDF`, `.Docx` work)
- [ ] Unknown extension (`.exe`, `.md`?, no-extension) → `DocumentError`
- [ ] Corrupt bytes for `.pdf`/`.docx` → `DocumentError` (not a raw library exception)
- [ ] `filename` is used ONLY for extension dispatch — content comes from `content`
- [ ] Module imports no Telegram/telebot code

**Verification:**
- `pip install -r requirements.txt`
- `python -m pytest tests/test_documents.py`
- `ruff check . && ruff format --check . && mypy .`

**Dependencies:** None. **Files:** `documents.py`, `requirements.txt`,
`tests/test_documents.py`. **Scope:** M

### Task 2: Bot wiring — document handler + message routing

**Description:** Four changes in `bot.py`, all backward-compatible:

1. **`build_document_handler(bot: telebot.TeleBot, kb: KbStack, conv:
   ConversationManager)`** → returns `handle_document(message, reply_to)` closure:
   - `doc = message.document`; `file_id = doc.file_id`;
     `file_name = doc.file_name or "document"` (documents may lack a name).
   - `file_info = bot.get_file(file_id)`;
     `content = bot.download_file(file_info.file_path)` (note: `download_file`
     takes the `File.file_path` string, not the file_id).
   - `text = documents.extract_text(content, file_name)`.
   - Build a fresh `KbIngestTool` with the same wiring as `build_tool_registry`
     (`store=kb.store, embedder=kb.embedder, vector_index=kb.vector_index,
     metadata=kb.metadata, chunk_size=kb.cfg.kb_chunk_size,
     chunk_overlap=kb.cfg.kb_chunk_overlap`) and call
     `.execute(text=text, title=file_name)`.
   - `reply_to(message, ingest_result)`.
   - Caption path: `caption = (getattr(message, "caption", None) or "").strip()`;
     if non-empty → `response = conv.handle(message.chat.id, caption)`;
     `reply_to(message, response)`. `LLMError` here → the same friendly
     "Sorry, I couldn't generate a response." reply used by `handle_message`.
   - Wrap download/extract/ingest in try/except for
     `telebot.apihelper.ApiTelegramException`, `documents.DocumentError`,
     `ToolError` → warning log (file_name/outcome only, no content) + one
     user-friendly reply. Never re-raise into the polling loop.
2. **`handle_message`** — signature becomes
   `handle_message(message, conv, reply_to, allowed_usernames=None,
   document_handler=None)` (new param LAST so existing positional call sites in
   tests are untouched). Auth check stays FIRST. Then:
   `doc = getattr(message, "document", None)`; if `doc` is present and
   `document_handler is None` → reply "Document uploads are not enabled." and
   return; if `doc` present and handler present → `document_handler(message,
   reply_to)` and return. Text path (`conv.handle(message.chat.id,
   message.text or "")`) untouched.
3. **`create_bot`** — signature becomes `create_bot(conv,
   allowed_usernames=None, kb=None)`. After creating the TeleBot instance:
   `document_handler = build_document_handler(bot, kb, conv) if kb is not None
   else None`; the `on_message` closure passes `document_handler` through.
4. **`main()`** — `create_bot(conv, allowed, kb=kb)` (kb is already composed there).

Write `tests/test_bot_documents.py` (offline; no real Telegram, no Ollama):
- Fake bot object (SimpleNamespace-style) with `get_file(file_id)` returning a fake
  File and `download_file(file_path)` returning canned bytes; records calls.
- Real kb stack over tmp SQLite mirroring `tests/test_kb_tools.py::_make_kb` but
  constructing a `KbStack` dataclass directly (local ~10-line `FakeEmbedder`
  subclass of `Embedder`; `RetrievalConfig(kb_chunk_size=…, kb_chunk_overlap=…)`;
  `VectorIndex`, `VectorIndexAdapter`, `StoreFtsAdapter`, `HybridRetriever`) —
  self-contained, no import from `tests.test_kb_tools`.
- Messages built as `SimpleNamespace` including `document=SimpleNamespace(file_id=…,
  file_name=…)` and `caption=…`, matching the repo's fake-message style.
- Caption path uses `ConversationManager(Agent(ScriptedLLM([ChatResponse(…)]),
  ToolRegistry()))` to prove the caption reached the agent.

**Acceptance criteria:**
- [ ] Document message → file downloaded (fake bot records get_file/download_file with
      the right file_id/file_path), extracted text ingested, confirmation reply sent
- [ ] Ingestion is real: a follow-up kb_search over the same tmp stack finds the
      document content
- [ ] Caption → exactly two replies in order: ingest confirmation, then agent response;
      agent's `chat_calls[0][0]` ends with the caption as the user message
- [ ] No caption → exactly one reply (the ingest confirmation)
- [ ] `document_handler=None` (kb off) + document present → "Document uploads are not
      enabled." reply, no download attempted
- [ ] Unknown extension / corrupt bytes → friendly error reply (no crash, no partial replies)
- [ ] Fake bot raising `ApiTelegramException` on get_file/download_file → friendly error reply
- [ ] Ingest `ToolError` (embedder failure) → friendly error reply
- [ ] Caption + `LLMError` → ingest confirmation still delivered, then the friendly
      LLM-error reply
- [ ] Unauthorized user + document → denial reply; download never attempted
- [ ] Messages WITHOUT a `document` attribute (existing SimpleNamespace fixtures) →
      text path unchanged (regression)
- [ ] `create_bot(conv, kb=None)` still constructs a working bot; `main()` passes `kb`

**Verification:**
- `python -m pytest tests/test_bot_documents.py`
- `python -m pytest` (full suite — existing bot/auth/agent tests must stay green)
- `ruff check . && ruff format --check . && mypy .`

**Dependencies:** Task 1. **Files:** `bot.py`, `tests/test_bot_documents.py`.
**Scope:** M

### Task 3: Documentation (`AGENTS.md`, `.env.example`, `skills/documents.md`)

**Description:** Concise docs matching how AGENTS.md documents the hybrid feature:
- `AGENTS.md`: add `documents.py` row to the project-structure table; extend the
  architecture diagram's Telegram arrow (document branch → extract → ingest →
  caption → agent); add a short "Documents" subsection under Architecture (bot-level
  ingestion rationale, supported extensions, 20 MB Telegram cap, two-reply caption
  UX, KB-disabled message); add one Testing bullet for the two new offline test
  files; note pypdf/python-docx in the stack line if appropriate.
- `.env.example`: **no new keys** (user decision). At most a one-line comment near the
  KB block noting that Telegram document uploads ingest into the KB when `KB_ENABLED=1`
  (20 MB Telegram cap, .txt/.pdf/.docx).
- `skills/documents.md` (new skill file — dropped into `skills/`, picked up by
  `SkillLoader` automatically): short instructions in the style of `skills/cve.md` —
  uploaded documents are ingested into the knowledge base automatically before you
  (the agent) run; when the user asks about an uploaded document, use `kb_search`
  with focused queries; never claim to read the raw file; if kb_search finds nothing,
  say so.
- Requirements note: document that `.txt/.pdf/.docx` are supported in the env table
  only if a natural home exists — no env-var table changes otherwise.

**Acceptance criteria:**
- [ ] `AGENTS.md` structure/architecture/testing sections match the new reality
- [ ] `.env.example` diff adds no variable bindings
- [ ] `skills/documents.md` exists, is instructions-only (no code), and
      `SkillLoader` picks it up (skill count increases — covered indirectly by
      `tests/test_skill_loader.py` patterns; no new test required)
- [ ] `python -m pytest` still green (skill prompt content flows into the agent
      system prompt only)

**Verification:** manual review; `git status` shows only intended files;
`python -m pytest` green.

**Dependencies:** Task 2. **Files:** `AGENTS.md`, `.env.example`,
`skills/documents.md`. **Scope:** S

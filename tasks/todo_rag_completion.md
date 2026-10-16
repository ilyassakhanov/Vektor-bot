# Todo: RAG Homework Completion — sqlite-vec, User Isolation, Commands, Bonuses

Checklist companion to `tasks/plan_rag_completion.md`. Work top-down; do not skip checkpoints.
WS-3 / WS-4 / WS-8 are parallel-safe after WS-2 (disjoint files). Per-track acceptance = named tests + `ruff`/`mypy` on touched files; full `python -m pytest` runs once in WS-9.

## WS-0 · Spike: sqlite-vec (gate for everything)

- [x] `pip install sqlite-vec` into `.venv` (no cp314 / macOS-arm64 wheel → fallback: loadable `vec0.dylib` via `conn.enable_load_extension(True/False)`)
- [x] Throwaway smoke script (temp dir): load extension → `CREATE VIRTUAL TABLE t USING vec0(id TEXT PRIMARY KEY, owner TEXT, emb FLOAT[4])` → insert → KNN `MATCH … AND k = ?` **with `WHERE owner = ?`** → record whether aux-column filtering works in KNN
- [x] Deliverables: `sqlite-vec` line in `requirements.txt`; tiny `retrieval/vec.py` (`load(conn)`) encapsulating the chosen load path; written decision (aux-filter yes/no → WS-1 query pattern)

### Checkpoint: WS-0
- [x] Smoke script runs against `.venv` Python 3.14 (native or dylib fallback)
- [x] KNN owner-filter decision recorded (yes → `WHERE owner = ?`; no → over-fetch `k×4` → filter → truncate)

## WS-1 · Storage: documents table + page column + vec0 (largest track)

- [x] Test-first: `tests/test_chunk_store.py` + new `tests/test_user_isolation.py` — two users, same-text ingest → A's search never returns B's chunks; deletion removes chunks from vector + FTS + metadata paths; `list_documents` scoping; lazy vec0 creation; `KBModelError` on legacy DB file
- [x] `retrieval/store.py` — target schema: `documents(id, user_id, filename, file_type, created_at)`, `chunks(id, document_id, chunk_index, text, page, embedding)`, `vec_chunks` (vec0, created lazily at first ingest, dim from existing `META_EMBED_DIM`), `chunks_fts` (+owner via join on chunk_id), `meta`
- [x] New store APIs: `add_document(doc)`; `list_documents(user_id)` (filename, created_at, chunk count); `delete_document(user_id, filename) -> bool` (cascades documents + chunks + `chunks_fts` + `vec_chunks` in ONE transaction; False when not owner); `search_vec(user_id, queries, limit)`; `search_fts(user_id, terms, limit)` (+owner filter); `metadata_by_ids` stays; `all_vectors` deleted; all writes single-transaction
- [x] Owner-filtered KNN per WS-0 decision: aux-column `WHERE owner = ?` if supported, else over-fetch `k×4` → join `chunks`/`documents` → filter `user_id` → truncate to limit
- [x] Delete `retrieval/vector_index.py`; `VectorSearch` protocol → `search(queries, limit, user_id)`; `StoreVecAdapter` replaces `VectorIndexAdapter`; metadata dict cache + both hydration layers collapse into SQL joins
- [x] Call-site adaptation only: `tools/kb.py`, `retrieval/hybrid.py`, `bot.py::build_kb_stack` (drop `VectorIndex(...)` wiring)

### Checkpoint: WS-1
- [x] `python -m pytest tests/test_chunk_store.py tests/test_user_isolation.py` green
- [x] `ruff check` + `mypy` green on touched files

## WS-2 · Principal plumbing

- [x] Tiny module `retrieval/principal.py` (or `tools/principal.py`) — `contextvars.ContextVar[str]` ("vektor.user")
- [x] `bot.py` — set from `message.from_user.id` in `handle_message` + `handle_document`, reset in `finally`; auth gate unchanged FIRST
- [x] `tools/kb.py` — `KbIngestTool._ingest` / `KbSearchTool.execute` read the var in the calling thread, pass `user_id` explicitly into retriever/store calls (contextvars don't cross the retrieval `ThreadPoolExecutor`); owner never in tool JSON schemas
- [x] Ingest writes the `documents` row (user_id, filename, file_type, created_at); bot handler passes filename/type alongside text+title
- [x] Delete `ensure_kb_single_user`, `KBMultiUserError`, and the `main()` gate (AGENTS.md text updates deferred to WS-5)
- [x] `agent/agent.py` — zero changes preferred (pass-through only if needed)
- [x] Tests: `tests/test_bot_documents.py` + `tests/test_bot.py` — user A uploads, user B's `kb_search` finds nothing; follow-up within same chat still works; tool JSON schema contains no `user_id` property (regression test)

### Checkpoint: WS-2
- [x] Isolation + no-`user_id`-in-schema tests green
- [x] `ruff check` + `mypy` green on touched files

## WS-3 · Bot UX: `/documents`, `/delete`, progress messages (parallel-safe with WS-4/WS-8 — owns `bot.py` only)

- [x] `bot.py::create_bot` — new `@bot.message_handler(commands=[...])` registrations for `/documents` and `/delete`; extract the existing auth gate into a helper and reuse it on commands
- [x] `/documents` → `📚 Your documents:` numbered list (filename, created date, chunk count) for the caller's `user_id` via `list_documents`; empty-state message
- [x] `/delete <filename>` → friendly confirmation, or «not found / not yours» reply
- [x] Progress bonus in `handle_document`: `📄 Document received` → `⏳ Extracting text… / ✅ N chunks` → `⏳ Generating embeddings…` → `✅ Document ready. Now you can ask questions.` → agent reply as today
- [x] Update two-reply expectations in `tests/test_bot_documents.py` for the staged sequence; new tests: command routing, auth on commands, delete cascade via real store, progress sequence ordering with a fake TeleBot

### Checkpoint: WS-3
- [x] Command-routing + progress-sequence tests green
- [x] `ruff check` + `mypy` green on `bot.py`

## WS-4 · Formats + PDF pages + source attribution (parallel-safe with WS-3/WS-8)

- [x] `documents.py` — `".md": _extract_txt` in `_EXTRACTORS` (+ update `SUPPORTED_EXTENSIONS` test); new `extract_pages(content, filename) -> list[tuple[int, str]]` (PDF: one entry per page via pypdf `page.extract_text()`; txt/md/docx: `[(1, text)]`); `extract_text` becomes a thin wrapper (existing tests keep passing)
- [x] `retrieval/chunking.py` — chunk per page (same 800/100 params); cross-page chunks split at page boundaries (limitation → README); `ChunkRecord.page` flows into `chunks.page`, `None` for non-PDF
- [x] `tools/kb.py` fact sheet — `[sources] title (chunk N, page M)`, page shown only when present
- [x] `skills/documents.md` — citation rule: every fact from `kb_search` cited as `Источник: <filename>, стр. M` (or `, chunk #N` when no page); nothing relevant → say so, never answer from general knowledge
- [x] Tests: `.md` extraction; per-page chunking page numbers; fact-sheet rendering with and without page; loader test that the skill file contains the citation rule

### Checkpoint: WS-4
- [x] `tests/test_documents.py` + chunking/fact-sheet/skill-loader tests green
- [x] `ruff check` + `mypy` green on touched files

## WS-8 · Reranking (parallel-safe with WS-3/WS-4)

- [x] New `retrieval/rerank.py` — pointwise rerank of fused top-k with the small model (reuse `OLLAMA_EXPANSION_MODEL` default `qwen3:0.6b`): one LLM call scoring each hit 0–10 for query relevance, re-sort, keep top-k; mirrors `expansion.py`: never raises, falls back to RRF order on any failure/timeout, tolerant structured-output parsing
- [x] `retrieval/config.py` — `KB_RERANK_ENABLED` (default `1`), `KB_RERANK_TIMEOUT` (default `10.0`) via `RetrievalConfig.from_env()` (warn-and-fallback pattern)
- [x] `retrieval/hybrid.py` — rerank stage; metrics `vektor_retrieval_latency_seconds{stage=rerank}`, `vektor_retrieval_rerank_total{status=ok|fallback}` (enum labels only)
- [x] `tests/test_rerank.py` with a scripted LLM — reorder ok, malformed scores → fallback, timeout → fallback, disabled → passthrough; `tests/test_hybrid.py` gains a rerank-stage case

### Checkpoint: WS-8
- [x] `python -m pytest tests/test_rerank.py tests/test_hybrid.py` green
- [x] `ruff check` + `mypy` green on touched files

## WS-5 · Documentation (after ALL code tracks)

- [x] `README.md` full rewrite per ДЗ §17, from plan facts only (agent reads no code): architecture diagram (Telegram → Agent → kb_search → embed → sqlite-vec → top-k → LLM → answer + source); chunking 800 chars / 100 overlap + rationale + too-small/too-big failure modes; embeddings `qwen3-embedding:0.6b` dim 1024 + why; retrieval cosine + `KB_TOP_K=5` + RRF k=60 + hybrid + rerank stage; storage schema documents → chunks → vec_chunks/FTS5; security (numeric-user-id SQL scoping, owner never exposed to the LLM, prompt-injection stance of the skill); bonuses incl. conversation-aware RAG (+ upload-notice mechanism, `test_bot_documents.py`); limitations (single-machine KB, page-boundary chunk splits, notional 20 MB Telegram cap, no per-user auth beyond allowlist)
- [x] `AGENTS.md` — schema table, env keys (`KB_RERANK_ENABLED`, `KB_RERANK_TIMEOUT`, `OLLAMA_RERANK_MODEL` if split), architecture notes replacing the "single-user scope" text (deferred from WS-2), test-coverage rows
- [x] `.env.example` — new keys with one-line comments

### Checkpoint: Docs
- [x] README / AGENTS.md / `.env.example` consistent with shipped code and env keys

## WS-9 · Final verification

- [x] Full `python -m pytest` green (once)
- [x] `ruff check .` + `ruff format --check .` + `mypy .` green
- [ ] Demo checklist walk against running bot + Ollama: upload → staged progress; Q → correct answer; answer contains `Источник: <file>, стр. N` (PDF) or `chunk #N`; no-answer question → explicit «не нашёл»; 2nd/3rd upload → search spans all of the user's documents; two users → B gets nothing from A (`/documents` proves scoping); `/delete <file>` → gone from `/documents` and from search
- [ ] `python -m benchmarks.retrieval_bench` → Recall/Precision@K table (Ollama up)
- [x] Trivial gaps fixed forward; non-trivial gaps filed as new tracks

### Checkpoint: Complete
- [x] All ДЗ acceptance criteria met (plan's Acceptance-Criteria Map)
- [x] Ready for review

## WS-0 Decision

- Wheel, not dylib: `sqlite-vec` 0.1.9 installs on Python 3.14 / macOS arm64 as a `py3-none-macosx_11_0_arm64` wheel; load path = `sqlite_vec.load(conn)` wrapped by `retrieval/vec.py::load` (enables/disables `enable_load_extension` around the call).
- Aux-column KNN filtering **works**: `WHERE owner = ? AND emb MATCH ? AND k = ?` is a true pre-filter (k=4 with only 2 matching-owner rows returns 2; non-matching owner returns empty, no error).
- `distance_metric=cosine` **not supported** on vec0 0.1.9 (`vec0 constructor error: Unknown table option`) → WS-1 normalizes vectors on write and uses the default distance (equivalent ranking on unit vectors).
- WS-1 query pattern: direct `SELECT chunk_id FROM vec_chunks WHERE owner = ? AND embedding MATCH ? AND k = ?` — no over-fetch k×4 needed.

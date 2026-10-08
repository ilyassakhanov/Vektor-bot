# Implementation Plan: RAG Homework Completion — sqlite-vec, User Isolation, Commands, Bonuses

## Overview

Close every remaining gap between the current Vektor implementation and the homework
«Домашнее задание: RAG для AI-агента». The core RAG pipeline (extraction, chunking,
embeddings, hybrid retrieval, agent tools, tests, eval bench) already exists; what is
missing are **six mandatory items** and **four bonus items**. The plan is written for
execution by AI sub-agents and is optimized for token usage: every track gets a fixed
file map and spec from this document — no agent re-explores the codebase.

## Assessment Snapshot (gaps as of today)

| # | Requirement (ДЗ) | Status | Evidence |
|---|---|---|---|
| 1 | User isolation (обязательное §10) | ❌ missing | No `user_id` anywhere; `bot.py:78` `ensure_kb_single_user` refuses multi-user startup instead of filtering; AGENTS.md calls per-user namespacing "future work" |
| 2 | `/documents` + `/delete` (§11) | ❌ missing | Only `/new` exists; no delete API on `ChunkStore` |
| 3 | `.md` support (§3) | ❌ missing | `documents.py:43` `_EXTRACTORS` = `.txt/.pdf/.docx` only |
| 4 | sqlite-vec (§6) | ❌ missing | `requirements.txt` has numpy; search is in-memory numpy cosine (`retrieval/vector_index.py`); `sqlite_vec` not installed |
| 5 | README (§17) | ❌ outdated | `README.md` (93 lines) describes the pre-RAG bot; no chunking/embeddings/retrieval/storage/security/limitations sections |
| 6 | Source attribution (§13) | ⚠️ partial | `kb_search` fact sheet shows `[sources] title (chunk N)` to the LLM, but `skills/documents.md` never instructs the model to cite sources in its answer |
| 7 | Bonus: progress messages (+1) | ❌ missing | Document flow sends exactly two replies, no staged progress |
| 8 | Bonus: PDF page numbers (+1) | ❌ missing | `extract_text` returns one string; chunks carry no page |
| 9 | Bonus: reranking (+1) | ❌ missing | Fusion ends at RRF top-k |
| 10 | Bonus: conversation-aware RAG (+2) | ✅ already | Conversation history + upload-notice turn recorded; only needs README documentation |

Everything else required by the assignment is already done: bot-level ingestion,
`kb_ingest`/`kb_search` as agent tools, multi-document support, error handling,
661 tests, 6-query eval bench (`benchmarks/retrieval_bench.py`), hybrid search
(vector ‖ FTS5 → RRF k=60), no-hallucination skill rule.

## Architecture Decisions (user-approved, fixed)

1. **Full sqlite-vec migration.** Delete the numpy `VectorIndex` path entirely; store
   and search vectors via a `vec0` virtual table inside the same SQLite DB. This is the
   literal acceptance criterion and it simplifies isolation (owner filtering in SQL) and
   deletes ~400 lines (`retrieval/vector_index.py`, `VectorIndexAdapter`, the
   `metadata` cache, the second hydration layer).
2. **`user_id` = Telegram numeric id** (`message.from_user.id`). Stable across
   `@username` changes. `ALLOWED_USERNAMES` stays as the auth gate only.
3. **Owner never controlled by the LLM.** `user_id` is absent from tool JSON schemas;
   it travels via a `contextvars.ContextVar` set at the handler boundary and is passed
   **down as an explicit parameter** (contextvars do not cross the retrieval
   `ThreadPoolExecutor` boundary).
4. **Isolation is SQL-level, not convention-level.** Every store search API takes a
   mandatory `user_id`; there is no unfiltered search entry point to call by accident.
5. **All four bonuses** (+1 progress, +1 pages, +1 rerank, +2 conversation-aware-docs).
6. **Fresh schema, no data migration.** Existing `data/vektor.db` is incompatible →
   the existing `KBModelError` pattern (startup refuses with "delete the DB file")
   covers the transition.
7. **Docstrings max 5 lines** in new/changed code (current codebase has very long
   docstrings — do not copy that style; it burns tokens on every later context load).

## Target Storage Schema

```sql
-- assignment §6 shape
documents(id TEXT PRIMARY KEY,          -- sha256(text)[:16], as today
          user_id TEXT NOT NULL,
          filename TEXT NOT NULL,
          file_type TEXT NOT NULL,      -- lowercased extension
          created_at TEXT NOT NULL)     -- ISO-8601 UTC

chunks(id TEXT PRIMARY KEY,             -- chunk_id_for(doc_id, idx), as today
       document_id TEXT NOT NULL REFERENCES documents(id),
       chunk_index INTEGER NOT NULL,
       text TEXT NOT NULL,
       page INTEGER,                    -- PDF only, NULL for txt/md/docx
       embedding BLOB)                  -- normalized float32

vec_chunks USING vec0(chunk_id TEXT PRIMARY KEY,
                      owner TEXT,       -- aux column for KNN filtering
                      embedding FLOAT[dim])  -- dim from meta 'embed_dim'

chunks_fts  -- FTS5, as today, + owner filtering via join on chunk_id
meta        -- as today (embed_model, embed_dim)
```

- `vec_chunks` is created **lazily at first ingest** (vec0 DDL needs a fixed dim; the
  dim is only known after the first embed — `META_EMBED_DIM` already exists for this).
- Vectors are normalized on write → cosine ranking (or `distance_metric=cosine` per
  the WS-0 spike result).
- KNN + owner filter: aux-column `WHERE owner = ?` in the MATCH query if the installed
  sqlite-vec supports it (spike decides); otherwise over-fetch `MATCH ... AND k = ?`
  with `k × 4` candidates → join `chunks`/`documents` → filter `user_id` → truncate to
  the requested limit (preserves top-k semantics).
- FTS + vector + fusion runs under the existing per-stack `ingest_lock` (unchanged).

## New Principal Flow

```
Telegram message ──► handle_message / handle_document   (auth gate unchanged, FIRST)
                        │  set ctx_user_id = from_user.id
                        ▼
              ConversationManager → Agent → ToolRegistry
                        │
              KbIngestTool / KbSearchTool               (read contextvar in caller thread)
                        │  pass user_id as explicit param
                        ▼
              HybridRetriever.search(query, user_id)
                        │  ThreadPoolExecutor workers receive user_id as arg
                        ▼
              store.search_vec(user_id, …) ‖ store.search_fts(user_id, …) → RRF → top-k
```

`ensure_kb_single_user` / `KBMultiUserError` are deleted — multi-user is now safe.

## Agent Workstreams

Sequencing (file-contention-safe: one writer per file at a time):

```
WS-0 spike ──► WS-1 storage ──► WS-2 principal ──┬── WS-3 bot UX        (bot.py)
 (sqlite-vec)   (store.py +      (bot.py,         ├── WS-4 formats+pages (documents.py,
                 vec0 schema)     tools/kb.py)    │                       chunking, kb sheet,
                                                  │                       skills/documents.md)
                                                  └── WS-8 reranking     (retrieval/rerank.py,
                                                                           hybrid.py, config.py)
WS-5 docs (README + AGENTS.md + .env.example)  ──► WS-9 final verification
```

Seven implementation tracks + docs + verify. Parallel branches WS-3 / WS-4 / WS-8 touch
disjoint files. Each track's acceptance = named tests + `ruff check` + `mypy` on the
touched files; the full `python -m pytest` runs once in WS-9.

---

### WS-0 · Spike: sqlite-vec on Python 3.14 (gate for everything)

- `pip install sqlite-vec` into `.venv` (risk: no cp314 / macOS-arm64 wheel).
- Smoke script (throwaway, `/var/folders/.../opencode` temp dir):
  load extension → `CREATE VIRTUAL TABLE t USING vec0(id TEXT PRIMARY KEY, owner TEXT,
  emb FLOAT[4])` → insert → KNN `MATCH ... AND k = ?` **with `WHERE owner = ?`** →
  report whether aux-column filtering works in KNN.
- Fallback if no wheel: download the loadable extension (`vec0.dylib` from sqlite-vec
  releases) and load via `conn.enable_load_extension(True/False)`.
- **Output:** one line added to `requirements.txt`; a tiny `retrieval/vec.py` helper
  (`load(conn)`) encapsulating the chosen load path; a written decision: aux-filter
  yes/no → WS-1 query pattern.

### WS-1 · Storage: documents table + page column + vec0 (largest track)

**Owns:** `retrieval/store.py` (rewrite vector path), **deletes**
`retrieval/vector_index.py`. Touches: `tools/kb.py`, `retrieval/hybrid.py`,
`bot.py::build_kb_stack` (call-site adaptation only).

- Implement the schema above; all writes single-transaction (chunks + FTS + vec0 +
  documents row), all reads owner-filtered.
- Store API (new signatures):
  - `add_document(doc) -> None`; `list_documents(user_id) -> list[DocumentRow]`
    (filename, created_at, chunk count);
  - `delete_document(user_id, filename) -> bool` — cascades documents + chunks +
    `chunks_fts` + `vec_chunks` rows in ONE transaction; returns False when the file
    does not belong to the user;
  - `search_vec(user_id, queries, limit)` (replaces the in-memory index);
    `search_fts(user_id, terms, limit)` gains the owner filter;
  - `metadata_by_ids` stays (hydration), `all_vectors` is deleted.
- `VectorSearch` protocol becomes `search(queries, limit, user_id)`;
  `StoreVecAdapter` replaces `VectorIndexAdapter`; the `metadata` dict cache and both
  hydration layers collapse into SQL joins. `bot.py` drops `VectorIndex(...)` wiring.
- Tests (`tests/test_chunk_store.py` + new `tests/test_user_isolation.py`):
  two users, same-text ingest → A's search never returns B's chunks; deletion removes
  chunks from vector, FTS, and metadata paths; `list_documents` scoping; lazy vec0
  creation; `KBModelError` on legacy DB file.

### WS-2 · Principal plumbing

**Owns:** `bot.py`, `tools/kb.py`. Touches: `agent/agent.py` (pass-through only if
needed — prefer zero agent changes).

- `contextvars.ContextVar[str]` ("vektor.user") in a tiny module
  (`retrieval/principal.py` or `tools/principal.py`); set in `handle_message` and
  `handle_document` from `message.from_user.id` (reset in `finally`).
- `KbIngestTool._ingest` and `KbSearchTool.execute` read the var in the calling thread
  and pass `user_id` explicitly into retriever/store calls.
- Ingest now also writes the `documents` row (user_id, filename, file_type,
  created_at) — the bot handler passes filename/type alongside text+title.
- Delete `ensure_kb_single_user`, `KBMultiUserError`, and the `main()` gate;
  update AGENTS.md claims only in WS-5 (avoid doc drift mid-series).
- Tests: `tests/test_bot_documents.py` + `tests/test_bot.py` — user A uploads, user B's
  `kb_search` finds nothing; follow-up within the same chat still works; tool JSON
  schema contains no `user_id` property (regression test).

### WS-3 · Bot UX: `/documents`, `/delete`, progress messages

**Owns:** `bot.py` (`create_bot`, `build_document_handler`), after WS-2.

- `/documents` → `📚 Your documents:` numbered list (filename, created date, chunk
  count) for the caller's `user_id`; empty state message.
- `/delete <filename>` → friendly confirmation, or «not found / not yours» reply.
  Handlers are new `@bot.message_handler(commands=[...])` registrations inside
  `create_bot`; auth check reused (extract the existing gate into a helper).
- Progress bonus: staged replies in `handle_document` —
  `📄 Document received` → `⏳ Extracting text… / ✅ N chunks` →
  `⏳ Generating embeddings…` → `✅ Document ready. Now you can ask questions.` —
  then the agent reply as today (two-reply invariant becomes N replies for big files;
  update the existing two-reply tests accordingly).
- Tests: command routing, auth on commands, delete cascade via real store, progress
  sequence ordering with a fake TeleBot.

### WS-4 · Formats + PDF pages + source attribution

**Owns:** `documents.py`, `retrieval/chunking.py`, `tools/kb.py` (fact sheet),
`skills/documents.md`. Parallel to WS-3/WS-8 (disjoint files).

- `".md": _extract_txt` in `_EXTRACTORS` (+ update `SUPPORTED_EXTENSIONS` test).
- New `extract_pages(content, filename) -> list[tuple[int, str]]` — PDF: one entry per
  page (pypdf `page.extract_text()`); txt/md/docx: `[(1, text)]`. `extract_text`
  becomes a thin wrapper (existing tests keep passing).
- Chunk per page (same 800/100 params); cross-page chunks are split at page
  boundaries (limitation → README). `ChunkRecord.page` flows into `chunks.page`;
  `None` for non-PDF.
- Fact sheet line becomes `[sources] title (chunk N, page M)` — page shown only when
  present.
- `skills/documents.md` gains: «Every fact you take from `kb_search` must cite its
  source as `Источник: <filename>, стр. M` (or `, chunk #N` when no page). If the
  search returns nothing relevant, say so — never answer from general knowledge.»
- Tests: `.md` extraction; per-page chunking page numbers; fact-sheet rendering with
  and without page; skill file contains the citation rule (loader test).

### WS-8 · Reranking

**Owns:** new `retrieval/rerank.py`, `retrieval/hybrid.py`, `retrieval/config.py`.
Parallel to WS-3/WS-4.

- Pointwise rerank of the fused top-k with the small model (reuse
  `OLLAMA_EXPANSION_MODEL` default `qwen3:0.6b`): one LLM call scoring each hit 0–10
  for query relevance, re-sort, keep top-k. Mirrors `expansion.py`: never raises,
  falls back to RRF order on any failure/timeout; structured output parsing tolerant.
- Env: `KB_RERANK_ENABLED` (default `1`), `KB_RERANK_TIMEOUT` (default `10.0`) via
  `RetrievalConfig.from_env()` (warn-and-fallback pattern).
- Metrics: `vektor_retrieval_latency_seconds{stage=rerank}`,
  `vektor_retrieval_rerank_total{status=ok|fallback}` (enum labels only).
- Tests: `tests/test_rerank.py` with a scripted LLM — reorder ok, malformed scores →
  fallback, timeout → fallback, disabled → passthrough; `tests/test_hybrid.py` gains a
  rerank-stage case.

### WS-5 · Documentation (after all code tracks)

**Owns:** `README.md` (full rewrite), `AGENTS.md`, `.env.example`.

- README sections per ДЗ §17, written from the facts in this document (the agent
  reads **no code**): architecture diagram (Telegram → Agent → kb_search → embed →
  sqlite-vec → top-k → LLM → answer + source); chunking 800 chars / 100 overlap +
  rationale + too-small/too-big failure modes; embeddings `qwen3-embedding:0.6b`,
  dim 1024, why (local, fast, strong multilingual retrieval); retrieval cosine +
  K=5 (`KB_TOP_K`) + RRF k=60 + hybrid + rerank stage; storage schema
  documents → chunks → vec_chunks/FTS5; security (numeric-user-id SQL scoping,
  owner never exposed to the LLM, prompt-injection stance of the skill); bonus
  features incl. conversation-aware RAG (+ upload-notice mechanism, tests
  `test_bot_documents.py`); limitations (single-machine KB, page-boundary chunk
  splits, notional 20 MB Telegram cap, no per-user auth beyond allowlist).
- AGENTS.md: schema table, env keys (`KB_RERANK_*`, new `OLLAMA_RERANK_MODEL` if
  split), architecture notes replacing "single-user scope" text, test-coverage rows.
- `.env.example`: new keys with one-line comments.

### WS-9 · Final verification

- Full `python -m pytest` (once), `ruff check .`, `ruff format --check .`, `mypy .`.
- Walk the ДЗ demo checklist (see below) manually against a running bot + Ollama;
  fix-forward anything trivial, file gaps as new tracks otherwise.

## Token-Optimization Rules (binding for every agent)

1. **No codebase re-exploration.** Prompts quote this plan's file maps, signatures,
   and schema; agents `Read` only the listed files.
2. **Tests-first for new store APIs** — small failing tests beat debug loops.
3. **Acceptance per track** = named test files + `ruff`/`mypy` on touched files;
   the full suite runs once (WS-9), not per track.
4. **Docstrings ≤ 5 lines** in new/changed code; no comments unless essential.
5. **Delete, don't wrap** — the numpy path, the single-user gate, and the metadata
   cache are removals, not shims.
6. **No reformatting** of untouched code (keeps diffs and review tokens small).
7. **Parallelism only on disjoint files** (WS-3 ∥ WS-4 ∥ WS-8).

## Risk Register

| Risk | Mitigation |
|---|---|
| No cp314 wheel for sqlite-vec | WS-0 runs first; fallback = loadable `vec0.dylib` via `enable_load_extension` |
| vec0 KNN lacks `WHERE owner` filtering | Over-fetch `k×4` → SQL join + owner filter → truncate (top-k preserved); decided in WS-0 |
| vec0 DDL needs fixed dim before any embed | Lazy table creation at first ingest; dim from `META_EMBED_DIM` (existing) |
| Contextvars don't cross executor threads | `user_id` passed as explicit function argument below the tool layer |
| Legacy `data/vektor.db` incompatible | `KBModelError` with "delete the DB file" guidance (existing pattern) |
| Two-reply tests break with progress messages | WS-3 updates `test_bot_documents.py` expectations in the same track |
| Rerank adds latency on every search | Behind `KB_RERANK_ENABLED`, short timeout, graceful fallback to RRF order |

## Acceptance-Criteria Map (ДЗ → track)

| Criterion | Covered by |
|---|---|
| Accepts .txt/.md/.docx/.pdf | existing + WS-4 |
| Text extraction, chunking, embeddings | existing |
| Embeddings in SQLite + sqlite-vec | WS-0 + WS-1 |
| Vector search | WS-1 (vec0 KNN) |
| Agent uses retrieval as a tool | existing (`kb_search`) |
| Multiple documents | existing |
| **User isolation** | WS-1 + WS-2 (+ `tests/test_user_isolation.py`) |
| **`/documents`** | WS-3 |
| **Deletion cascades chunks + embeddings** | WS-1 (API) + WS-3 (UX) |
| **Source attribution (+ PDF pages)** | WS-4 |
| Agent doesn't hallucinate | existing skill rule + WS-4 citation rule |
| Error handling | existing + WS-3 progress errors |
| ≥5 automated tests / ≥5 eval questions | existing (661 tests; 6 bench queries) + per-track tests |
| **README (architecture + decisions)** | WS-5 |
| Bonuses: progress / pages / hybrid / rerank / conversation-aware | WS-3 / WS-4 / existing / WS-8 / WS-5 (docs) |

## Demo Checklist (ДЗ «Что нужно продемонстрировать», run in WS-9)

1. Upload a document via Telegram → staged progress replies.
2. Question about the document → correct answer.
3. Answer contains `Источник: <file>, стр. N` (PDF) or `chunk #N`.
4. Question with no answer in documents → explicit «не нашёл» reply.
5. Second/third upload → search spans all of the user's documents.
6. Two users → B gets nothing from A's documents (`/documents` proves scoping).
7. `/delete <file>` → document gone from `/documents` and from search.
8. `python -m pytest` green; `ruff`/`mypy` clean.
9. `python -m benchmarks.retrieval_bench` → Recall/Precision@K table (Ollama up).

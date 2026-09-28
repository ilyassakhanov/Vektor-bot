# Implementation Plan: Hybrid Search + Query Expansion for Vektor

## Overview

Add a local, privacy-first knowledge base to Vektor with hybrid retrieval: LLM query
expansion (small Qwen via the existing `LLM` interface) → vector search (Ollama
embeddings + numpy cosine) ‖ full-text search (SQLite FTS5) → Reciprocal Rank Fusion
(RRF) → Top-K. Exposed to the agent as `kb_ingest` / `kb_search` tools through the
existing `ToolRegistry`. The agent loop, MCP CVE server, and conversation flow remain
untouched.

## Reality Check (repo fit)

The feature spec targets a repo with an existing RAG pipeline. **Vektor has none** —
no chunks, embeddings, SQLite, or ingestion (verified: only CVSS `attack_vector`
grep false-positives). This plan therefore adds the minimal substrate the spec
presumes, following Vektor's conventions: provider code behind ABCs, composition in
`bot.py`, tools via the `Tool` ABC, `vektor_*` metrics, network-free tests with
fakes + `httpx.MockTransport`. Vektor is fully synchronous (sync httpx, sync
`Tool.execute`) → parallel retrieval uses `ThreadPoolExecutor` (the spec's sanctioned
fallback), not asyncio.

## Architecture Decisions

1. **Single SQLite DB** (`data/vektor.db`) holds the chunks table + FTS5 virtual
   table + embedding BLOBs — one store; FTS always in sync (same transaction). FTS5
   availability probed at init; unavailable → automatic vector-only mode + warning.
2. **Expansion depends only on `LLM.generate()`** — a second `OllamaLLM` instance
   with `OLLAMA_EXPANSION_MODEL` (default `qwen3:0.6b`), temperature 0, short
   timeout. Any failure/timeout/malformed/empty output → original query; expansion
   can never fail retrieval.
3. **Embeddings** via a new `OllamaEmbedder` (`/api/embed`, injectable httpx client,
   mirroring `OllamaLLM` patterns). Default model `qwen3-embedding:0.6b` (user
   decision). numpy added to requirements (user decision).
4. **Stable chunk IDs**: `sha256(doc_id + chunk_index)` — deterministic re-ingest
   upserts instead of duplicating; RRF merges by this ID.
5. **RRF is a pure function** `rrf_fuse(rankings, k=60, top_k)` — score
   `+= 1/(k+rank)` (1-based rank); tie-break `(-score, chunk_id)` for determinism;
   raw scores never mixed; source metadata preserved.
6. **Agent tools only** (user decision): `kb_ingest(text, title?)`,
   `kb_search(query)`. No CLI. Registered in `build_tool_registry()` when
   `KB_ENABLED=1` (default on — user decision).
7. **Pipeline position**: `kb_search` is the retrieval entry — its formatted
   fact-sheet result feeds back into the agent conversation (Vektor's RAG loop).

## Pipeline

```
Query → Expansion (LLM.generate, small Qwen)
      → ThreadPoolExecutor[ Vector ‖ FTS ]
      → RRF fusion → Top-K fact sheet → agent conversation → LLM/MCP
```

## Task List

### Phase 1: Foundation (pure, no LLM/network)

- [ ] Task 1: `retrieval/rrf.py` — RRF fusion pure function
- [ ] Task 2: `retrieval/store.py` — ChunkStore (SQLite chunks + FTS5, stable IDs)
- [ ] Task 3: `retrieval/chunking.py` + `retrieval/vector_index.py` — chunker + numpy cosine search

### Checkpoint: Foundation

- [ ] pytest, ruff check, ruff format --check, mypy all green

### Phase 2: Provider touchpoints (mocked)

- [ ] Task 4: `retrieval/embeddings.py` — OllamaEmbedder + `temperature` option in OllamaLLM
- [ ] Task 5: `retrieval/expansion.py` — QueryExpander with fallback

### Checkpoint: Providers

- [ ] Full suite green; expansion fallback demonstrated

### Phase 3: Core integration

- [ ] Task 6: `retrieval/config.py` — RetrievalConfig.from_env()
- [ ] Task 7: `retrieval/hybrid.py` — HybridRetriever (parallel + one-source failure + vector-only)
- [ ] Task 8: `tools/kb.py` + `bot.py` wiring — kb_ingest/kb_search tools behind KB_ENABLED
- [ ] Task 9: Observability — vektor_retrieval_* metrics

### Checkpoint: Core

- [ ] Full suite + lint/mypy green
- [ ] Manual smoke with live Ollama: ingest via chat, search via chat

### Phase 4: Polish

- [ ] Task 10: `benchmarks/retrieval_bench.py` — vector-only vs hybrid vs hybrid+expansion
- [ ] Task 11: Documentation — AGENTS.md, .env.example, .gitignore

### Checkpoint: Complete

- [ ] All acceptance criteria met
- [ ] Ready for review

## Configuration (centralized in `retrieval/config.py`)

| Key | Default | Purpose |
|---|---|---|
| `KB_ENABLED` | `1` | register kb tools (0 = today's exact behavior) |
| `KB_DB_PATH` | `data/vektor.db` | SQLite file (chunks + FTS5 + vectors) |
| `KB_CHUNK_SIZE` / `KB_CHUNK_OVERLAP` | `800` / `100` | chunker |
| `KB_VECTOR_LIMIT` / `KB_FTS_LIMIT` / `KB_TOP_K` | `20` / `20` / `5` | retrieval limits |
| `KB_RRF_K` | `60` | RRF k |
| `KB_FTS_ENABLED` | `1` | 0 = vector-only fallback |
| `KB_EXPANSION_ENABLED` | `1` | 0 = skip expansion |
| `OLLAMA_EXPANSION_MODEL` | `qwen3:0.6b` | small Qwen for expansion |
| `KB_EXPANSION_TIMEOUT` / `KB_EXPANSION_TEMPERATURE` | `10` / `0.0` | expansion tuning |
| `OLLAMA_EMBED_MODEL` | `qwen3-embedding:0.6b` | embedder model |

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| FTS5 missing in SQLite build | Med | Runtime probe → vector-only mode + warning log |
| Embed/expansion Ollama models not pulled | Med | Tool errors surfaced to LLM via registry; FTS still answers; document `ollama pull` in AGENTS.md |
| Expansion adds per-query latency | Med | 10s timeout, per-stage metrics, easy toggle |
| numpy addition | Low | User-approved; `numpy>=1.26` |
| SQLite write contention (ingest ‖ search) | Low | Single process, short transactions, lock around writes |

## Open Questions

None — resolved: agent tools only, qwen3-embedding:0.6b, numpy, enabled by default.

## Detailed Task Specs

### Task 1: RRF fusion (`retrieval/rrf.py`)

**Description:** Pure `rrf_fuse(rankings: dict[str, list[ChunkHit]], k, top_k) -> list[FusedHit]`.
Score contribution `1/(k+rank)` with 1-based rank; merge by stable chunk id; keep
source attribution (vector/fts); deterministic tie-break `(-score, chunk_id)`; return
top-k. Raw scores never mixed.

**Acceptance criteria:**
- [ ] Formula exact for single and dual appearances
- [ ] Duplicates merged into one hit with combined score
- [ ] Equal-score order deterministic (chunk id tie-break)
- [ ] Top-K respected

**Verification:** `python -m pytest tests/test_rrf.py`

**Dependencies:** None. **Files:** `retrieval/rrf.py`, `retrieval/__init__.py`,
`tests/test_rrf.py`. **Scope:** S

### Task 2: ChunkStore (`retrieval/store.py`)

**Description:** SQLite store: `chunks` table (id, doc_id, title, idx, content,
embedding BLOB) + standalone FTS5 virtual table keyed by chunk id. Stable ids =
`sha256(doc_id + idx)`. `add_chunks` upserts both tables in one transaction
(`INSERT OR REPLACE` + FTS delete/insert). `search_fts(terms, limit)` (BM25 order),
`all_vectors()`, `count()`. FTS5 probed at init. No SQL outside this module.

**Acceptance criteria:**
- [ ] Insert/upsert keeps chunks and FTS tables synced
- [ ] Exact and rare-term matches found; no results for unmatched terms
- [ ] Stable IDs → re-ingest overwrites, never duplicates
- [ ] Missing FTS5 → capability probe result, callers can degrade

**Verification:** `python -m pytest tests/test_chunk_store.py` (tmp_path DB)

**Dependencies:** None. **Files:** `retrieval/store.py`, `tests/test_chunk_store.py`.
**Scope:** M

### Task 3: Chunker + vector search (`retrieval/chunking.py`, `retrieval/vector_index.py`)

**Description:** Pure `chunk_text(text, size, overlap)` — whitespace-friendly
fixed-size windows with overlap. `VectorIndex`: numpy cosine similarity over vectors
loaded from ChunkStore BLOBs; in-process cache invalidated on write;
`search(query_vec, limit)` → ranked hits with scores.

**Acceptance criteria:**
- [ ] Chunk boundaries/overlap correct; empty/short input handled
- [ ] Cosine ordering correct with fake embeddings
- [ ] BLOB round-trip lossless (float32)
- [ ] Cache invalidated after ingest

**Verification:** `python -m pytest tests/test_chunking.py tests/test_vector_index.py`

**Dependencies:** Task 2. **Files:** `retrieval/chunking.py`,
`retrieval/vector_index.py`, `requirements.txt` (+numpy), 2 test files. **Scope:** M

### Task 4: OllamaEmbedder + LLM temperature (`retrieval/embeddings.py`, `llm/ollama.py`)

**Description:** `Embedder` ABC + `OllamaEmbedder` posting to `/api/embed`
(injectable httpx.Client, MockTransport-testable; timeout/HTTP/connect errors →
`EmbeddingError`). Add backward-compatible `temperature: float | None` to
`OllamaLLM` merged into payload `options` (only when set).

**Acceptance criteria:**
- [ ] Embed request payload/response parsed correctly (incl. usage fields)
- [ ] Timeout/HTTP/connect errors → EmbeddingError
- [ ] Temperature appears in payload only when set; existing behavior unchanged
- [ ] Model/base-url configurable

**Verification:** `python -m pytest tests/test_ollama_embeddings.py tests/test_ollama.py`

**Dependencies:** None. **Files:** `retrieval/embeddings.py`, `llm/ollama.py` (small
edit), `tests/test_ollama_embeddings.py`, `tests/test_ollama.py`. **Scope:** M

### Task 5: QueryExpander (`retrieval/expansion.py`)

**Description:** Small-model expansion prompt via `LLM.generate()`: keywords/phrases
only, minimal JSON (`{"keywords": [...], "queries": [...]}`), no explanations,
temperature 0. Parser accepts JSON or comma-separated fallback; caps 8 keywords +
3 alt queries. On LLMError/timeout/malformed/empty → original query only; never
raises. Result type: `ExpandedQuery(original, keywords, alt_queries, used_expansion)`.

**Acceptance criteria:**
- [ ] Valid JSON, keywords-only, comma-separated inputs parsed
- [ ] Malformed / empty / LLMError / timeout → fallback to original
- [ ] No exception ever escapes; fallback path covered by tests

**Verification:** `python -m pytest tests/test_expansion.py` (FakeLLM)

**Dependencies:** Task 4. **Files:** `retrieval/expansion.py`,
`tests/test_expansion.py`. **Scope:** S

### Task 6: RetrievalConfig (`retrieval/config.py`)

**Description:** Frozen `RetrievalConfig` dataclass + `from_env()` reading every key
in the configuration table; invalid values → warning + default (mirrors
`_metrics_port_from_env` style). Single place where env is read.

**Acceptance criteria:**
- [ ] All keys read with documented defaults
- [ ] Invalid values → default + warning, never crash
- [ ] Boolean/int/float parsing covered by tests

**Verification:** `python -m pytest tests/test_retrieval_config.py`

**Dependencies:** None. **Files:** `retrieval/config.py`,
`tests/test_retrieval_config.py`. **Scope:** S

### Task 7: HybridRetriever (`retrieval/hybrid.py`)

**Description:** Orchestrates: expand (if enabled) → embed query →
`ThreadPoolExecutor(max_workers=2)` running vector search ‖ FTS (queries built from
original + keywords + alt queries) → `rrf_fuse` → Top-K. One source failure → other
source's ranking alone; both fail → empty result + note. `KB_FTS_ENABLED=0` →
vector-only. Searchable thin interfaces (`FtsSearch`, `VectorSearch`) so no SQLite
leaks here.

**Acceptance criteria:**
- [ ] Both sources run concurrently (spy/latency proof, no forced sequencing)
- [ ] Single-source failure handled; hybrid still returns results
- [ ] Expanded terms provably reach both FTS and vector queries
- [ ] Vector-only mode works; Top-K respected; source metadata kept

**Verification:** `python -m pytest tests/test_hybrid.py` (FakeLLM + fake embedder +
tmp SQLite)

**Dependencies:** Tasks 1, 2, 3, 5, 6. **Files:** `retrieval/hybrid.py`,
`tests/test_hybrid.py`. **Scope:** M

### Task 8: kb tools + wiring (`tools/kb.py`, `bot.py`)

**Description:** `KbIngestTool` (name `kb_ingest`; params text, title?) — chunk →
embed → store; returns counts + chunk ids. `KbSearchTool` (name `kb_search`; params
query) — hybrid pipeline → compact fact sheet (title, chunk idx, source tags,
scores not shown raw), capped via existing `truncate()`. `build_retriever()` in
bot.py composes embedder + store + expander + retriever (expansion LLM =
second OllamaLLM with `OLLAMA_EXPANSION_MODEL`, temperature, short timeout);
`build_tool_registry()` registers both tools when `KB_ENABLED=1` (default).

**Acceptance criteria:**
- [ ] Ingest→search roundtrip works with fakes (tmp DB)
- [ ] `KB_ENABLED=0` → tool list identical to today
- [ ] Agent end-to-end test: user message → agent calls kb_search → final answer
- [ ] MCP untouched; existing MCP/bot tests green

**Verification:** `python -m pytest tests/test_kb_tools.py tests/test_agent.py tests/test_mcp_tool.py tests/test_bot_agent.py`

**Dependencies:** Task 7. **Files:** `tools/kb.py`, `bot.py`,
`tests/test_kb_tools.py`. **Scope:** M

### Task 9: Observability (`metrics.py`, `retrieval/hybrid.py`)

**Description:** New `vektor_*` metrics following existing style:
`vektor_retrieval_expansion_total{status=ok|fallback}`,
`vektor_retrieval_latency_seconds{stage=expansion|vector|fts|total}`,
`vektor_retrieval_results{source=vector|fts|final}` (histogram of counts). Tool
latency for kb_* already covered by `InstrumentedToolRegistry`. No sensitive query
text in metrics or logs. No new tracing stacks.

**Acceptance criteria:**
- [ ] Metrics recorded on success and fallback paths
- [ ] No query text/chunk content in metric labels or logs

**Verification:** `python -m pytest tests/test_metrics.py tests/test_hybrid.py`

**Dependencies:** Task 7. **Files:** `metrics.py`, `retrieval/hybrid.py`, test
updates. **Scope:** S

### Task 10: Retrieval benchmark (`benchmarks/retrieval_bench.py`)

**Description:** Small labeled corpus + query set; compares vector-only | hybrid |
hybrid+expansion. Metrics: relevant hits, Recall@K, Precision@K where labeled,
latency per stage (retrieval + expansion). Requires live Ollama; excluded from
pytest; no quality claims without an actual run. Unit test covers scoring math only.

**Acceptance criteria:**
- [ ] Three modes runnable from one entry point; prints a metrics table
- [ ] Offline unit test for metric math (no network)

**Verification:** `python -m pytest tests/test_retrieval_bench.py`; optional live:
`python -m benchmarks.retrieval_bench`

**Dependencies:** Task 8. **Files:** `benchmarks/retrieval_bench.py`,
`tests/test_retrieval_bench.py`. **Scope:** M

### Task 11: Documentation (`AGENTS.md`, `.env.example`, `.gitignore`)

**Description:** Concise updates: hybrid architecture + pipeline diagram, expansion
behavior/fallbacks, FTS storage/index sync, RRF, new config rows, `ollama pull`
prereqs, test run instructions, benchmark instructions, `.gitignore` `data/`.

**Acceptance criteria:**
- [ ] Every new env var documented with default
- [ ] Pipeline + fallbacks documented; docs match code defaults
- [ ] `data/` gitignored

**Verification:** manual review; `git status` shows only intended files

**Dependencies:** Task 8. **Files:** `AGENTS.md`, `.env.example`, `.gitignore`.
**Scope:** S

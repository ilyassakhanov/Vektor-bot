# Todo: Hybrid Search + Query Expansion for Vektor

Checklist companion to `tasks/plan.md`. Work top-down; do not skip checkpoints.

## Phase 1: Foundation (pure, no LLM/network)

- [x] Task 1: `retrieval/rrf.py` — RRF fusion pure function (+ `tests/test_rrf.py`)
- [x] Task 2: `retrieval/store.py` — ChunkStore: SQLite chunks + FTS5, stable sha256 ids, atomic sync (+ `tests/test_chunk_store.py`)
- [x] Task 3: `retrieval/chunking.py` + `retrieval/vector_index.py` — chunker + numpy cosine search; add numpy to requirements (+ 2 test files)

### Checkpoint: Foundation
- [ ] `python -m pytest` green
- [ ] `ruff check .` + `ruff format --check .` green
- [ ] `mypy .` green

## Phase 2: Provider touchpoints (mocked)

- [x] Task 4: `retrieval/embeddings.py` — Embedder ABC + OllamaEmbedder (`/api/embed`, injectable client); backward-compatible `temperature` option in `llm/ollama.py` (+ `tests/test_ollama_embeddings.py`, extend `tests/test_ollama.py`)
- [x] Task 5: `retrieval/expansion.py` — QueryExpander: small-Qwen prompt, temp 0, JSON/comma parsing, never-fail fallback (+ `tests/test_expansion.py`)

### Checkpoint: Providers
- [ ] Full suite + lint/mypy green
- [ ] Expansion fallback proven (malformed/empty/error/timeout → original query)

## Phase 3: Core integration

- [x] Task 6: `retrieval/config.py` — RetrievalConfig.from_env(): all KB_* / OLLAMA_EXPANSION_* / OLLAMA_EMBED_MODEL knobs, warn+default on invalid (+ `tests/test_retrieval_config.py`)
- [x] Task 7: `retrieval/hybrid.py` — HybridRetriever: expand → ThreadPoolExecutor(vector ‖ FTS) → RRF → Top-K; one-source failure tolerated; vector-only mode (+ `tests/test_hybrid.py`)
- [x] Task 8: `tools/kb.py` — KbIngestTool + KbSearchTool; `bot.py`: build_retriever() + registry wiring behind KB_ENABLED=1 default (+ `tests/test_kb_tools.py`)
- [ ] Task 9: Observability — `vektor_retrieval_*` metrics (expansion ok/fallback, per-stage latency, result counts); no sensitive text in labels/logs

### Checkpoint: Core
- [ ] Full suite + lint/mypy green
- [ ] `KB_ENABLED=0` → tool list identical to today (regression check)
- [ ] MCP tests green, untouched
- [ ] Manual smoke with live Ollama: paste text → agent ingests → agent answers via kb_search

## Phase 4: Polish

- [ ] Task 10: `benchmarks/retrieval_bench.py` — vector-only | hybrid | hybrid+expansion (Recall@K, Precision@K, latency); offline math test (+ `tests/test_retrieval_bench.py`)
- [ ] Task 11: Docs — AGENTS.md (env table, structure, architecture, testing), `.env.example`, `.gitignore` (`data/`)

### Checkpoint: Complete
- [ ] All acceptance criteria met (see plan.md acceptance mapping)
- [ ] Ready for review

## Acceptance (1:1 with spec)

- [ ] Vector search still works (incl. `KB_FTS_ENABLED=0` vector-only mode)
- [ ] Expansion runs (when enabled) before retrieval, small-model prompt, temp 0
- [ ] Expansion failure → original query (never fails retrieval)
- [ ] FTS5 synced with storage (single transaction)
- [ ] Concurrent vector+FTS (ThreadPoolExecutor; Vektor is sync)
- [ ] RRF merges correctly (k=60 default, stable ids, no raw-score mixing)
- [ ] Top-K respected
- [ ] Vector-only available
- [ ] Config centralized
- [ ] Unit + integration tests pass, no Ollama/network needed
- [ ] MCP intact
- [ ] Docs updated
- [ ] No unrelated architecture changes

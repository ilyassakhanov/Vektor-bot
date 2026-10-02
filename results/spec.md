<task>
Implement the Hybrid Search + Query Expansion assignment in this repository.

First read `AGENTS.md` and inspect the existing RAG, Ollama, indexing, and MCP code. Treat `AGENTS.md` as authoritative. Implement the feature completely; do not stop at a design or plan.

<requirements>
- Add an isolated, testable Ollama query-expansion step before retrieval.
- Generate a small number of keywords/phrases or 2–3 alternative queries.
- Optimize the prompt for Qwen 0.6B–3B; temperature 0–0.1.
- Always retain the original query.
- Invalid/empty output, timeout, or LLM failure → silently fall back to the original query.

* Add persistent SQLite FTS5 over the existing chunks.

* Reuse existing ingestion/chunking/index data; do not duplicate it.

* Rebuild/update FTS whenever the existing explicit RAG index rebuild occurs.

* Return stable chunk IDs + metadata suitable for fusion.

* Keep paths compatible with the existing `src/` layout.

* Do not replace FAISS.

* After expansion, run FAISS and FTS concurrently using a simple mechanism suitable for the synchronous application (`ThreadPoolExecutor` preferred).

* A failure in one backend must not discard successful results from the other.

* Implement independent RRF:
  `score(d) = Σ 1 / (60 + rank(d))`

* Fuse both rankings, deduplicate by stable chunk ID, sort by score, and return configurable Top-K.

* Feed the hybrid results into the existing final-answer/MCP pipeline.

<preservation>
Do not unnecessarily rewrite existing architecture, MCP protocol/client, packaging, or unrelated code. Prefer stdlib and existing dependencies. Python 3.10+, type hints, focused functions, clear error handling.
</preservation>

<tests>
Use TDD. Add focused tests for query-expansion parsing/fallback, FTS indexing/search, RRF correctness, hybrid retrieval, concurrency, partial backend failure, and pipeline wiring.

Tests must mock Ollama, FAISS, embeddings, filesystem-heavy operations, and other expensive/external dependencies. They must run without Ollama or model downloads.

Do not weaken tests to make them pass. </tests>

<docs>
Update the appropriate developer documentation for the new architecture, FTS index/rebuild behavior, configuration, and test commands.
</docs>

<verification>
Run the complete test suite and all available type/lint checks. Exercise index building and retrieval. Verify FTS5 is used, FAISS+FTS actually execute concurrently, expansion failure falls back correctly, and existing MCP behavior still works. Inspect the final diff and remove unrelated changes. Fix all failures before finishing.
</verification>

<deliver>
Return only a concise implementation summary, changed files, verification results, and remaining limitations.
</deliver>

---Fix prompt-1-plan-mode----
Have a look at @tasks/ . The project would have to have a RAG, unfortunately planning didn't include functionality to send documents to telegram bot. What would it take so RAG and bot would work with .pdf, .txt and maybe .docx files? 
-----------------------------

------Second-fix-plan-mode----
have a look at this PR, this change had to add a rag and document ingest. Document ingest isn't working. I have started up the bot and sent a pdf doc using the chat and nothing happened. Investigate why

------------------------------
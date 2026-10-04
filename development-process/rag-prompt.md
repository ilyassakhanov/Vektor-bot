<role>
Senior Python engineer extending local-rag-mcp. Inspect repo first; adapt to existing architecture. Do not redesign unrelated parts.
</role>

<context>
Local privacy-first Python RAG+MCP: Ollama/Qwen LLMs, doc ingestion/chunking, embeddings+vector search, optional MCP tools, Python 3.10+, existing conventions/tests.
Add Hybrid Search + Query Expansion without breaking RAG/MCP.
</context>

<goal>
Improve recall/precision via:
1. LLM query expansion
2. existing vector search
3. full-text search
4. Reciprocal Rank Fusion (RRF)
Keep simple, local; preserve existing pipeline.
</goal>

<requirements>

<query_expansion>
Pre-retrieval step using existing Ollama/LLM.
Prompt for small Qwen (0.6B–3B): short/deterministic, temp 0 or 0.1, keywords/phrases only, comma-separated or minimal JSON, no explanations.
Result: original query + keywords/phrases + optional 2–3 alt queries.
Optional: on LLM fail/timeout/invalid/empty → fallback to original query. Never fail retrieval solely due to expansion.
</query_expansion>

<fts>
Full-text search over existing chunks. Prefer SQLite FTS5 if compatible; extend existing SQLite, do not add new DB.
Keep FTS index synced with chunks.
Search original query + keywords + expanded queries.
Abstract behind thin interface (no SQLite leakage into retrieval logic).
</fts>

<parallel_retrieval>
After expansion: run vector + FTS concurrently (asyncio.gather preferred; ThreadPoolExecutor only if blocking needed). Match existing async/sync model. Avoid extra concurrency infra. FTS must not force full sequential latency.
</parallel_retrieval>

<hybrid_fusion>
RRF: default k=60 (configurable).
RRF(d) += 1/(k+rank)
Merge by stable chunk/doc ID. Return Top-K. Never mix raw scores. Keep source metadata.
</hybrid_fusion>

<retrieval_pipeline>
Query → Expansion → (Vector ‖ FTS) → RRF → Top-K → existing RAG → LLM/MCP
</retrieval_pipeline>

<configuration>
Centralize (no hardcoding):
- FTS on/off
- expansion on/off
- vector limit, FTS limit, final Top-K
- RRF k
- expansion temperature
Sensible defaults matching project. Vector-only remains available fallback.
</configuration>

<architecture>
Follow existing structure/abstractions.
Before coding inspect:
1. ingestion/storage
2. chunk representation + stable IDs
3. vector index/search
4. Ollama/LLM integration
5. RAG retrieval entry
6. MCP tools
7. tests
Reuse interfaces. No duplicate storage. No new frameworks.
</architecture>

<testing>
TDD. Mock/fake Ollama (no live server/network).

Query expansion: valid keywords, minimal JSON, malformed, empty, fail/timeout, fallback.

FTS: create/update index, insert, exact/rare matches, no results.

Parallel: both run, one-source failure handled, no forced sequential dep.

RRF: dual appearance combines ranks, formula correct, dups merged, equal-score deterministic order, Top-K respected.

Integration: expansion feeds retrieval, terms reach FTS/vector, hybrid reaches RAG, vector-only works, MCP unaffected.
</testing>

<benchmark>
Extend existing eval if present; else small separate plan.
Compare: vector-only | hybrid | hybrid+expansion.
Metrics: relevant hits, Recall@K, Precision@K (if data), latency (retrieval + expansion).
No quality claims without evidence.
</benchmark>

<observability>
Use existing instrumentation (Grafana/Prometheus/Loki subproject). Expose if points already exist: mode, expansion success, vector/FTS/final counts, latencies. Do not add OTel/Tempo/new tracing.
</observability>

<quality>
Python 3.10+, type hints, small focused fns, clear interfaces, minimal abstractions, no duplicated logic, no global mutable state, unit tests network-free, practical backwards compat. No unrelated changes.
</quality>

<documentation>
Concise updates: hybrid arch, expansion behavior, FTS storage/index, RRF, config, fallbacks, test run, vector vs hybrid comparison.
</documentation>

<acceptance>
Done when:
[ ] Vector search still works
[ ] Expansion runs (when enabled) before retrieval
[ ] Small-model prompt used
[ ] Expansion fail → original query
[ ] FTS5 synced with existing storage
[ ] Concurrent vector+FTS
[ ] RRF merges correctly
[ ] Top-K respected
[ ] Vector-only available
[ ] Config centralized
[ ] Unit+integration tests pass
[ ] MCP intact
[ ] Tests need no Ollama/network
[ ] Docs updated
[ ] No unrelated architecture

Report: files changed, arch deltas, tests added, results, config added, benchmarks (if any), remaining limits.
</acceptance>

<constraints>
Prefer fit to repo over blind assignment. Prefer SQLite FTS5 if SQLite already used. Do not rewrite RAG, replace vector store, add external/hosted/NVD services. All local. Smallest clean solution.
</constraints>
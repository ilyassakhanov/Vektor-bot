# Delta: kb-retrieval

## ADDED Requirements

### Requirement: Hybrid retrieval pipeline

`kb_search` SHALL run a hybrid pipeline: optional LLM query expansion, one
batched embed call for the original plus alternative queries, vector and FTS
search in parallel over each query's results, Reciprocal Rank Fusion with
`KB_RRF_K` (default 60), then top-k (`KB_TOP_K`, default 5), then an
optional pointwise LLM rerank of the fused hits. Raw per-source scores SHALL
never be mixed: fusion ranks only.

#### Scenario: Both sources contribute to the fused order

- **WHEN** a chunk is found by both vector and FTS search
- **THEN** its fused RRF score reflects both ranks, and the final order comes from fused ranks, not from raw scores

#### Scenario: Alternatives are embedded in one batch

- **WHEN** expansion produces alternative queries
- **THEN** the embedder receives the original and all alternatives in a single batch call

### Requirement: Expansion keywords stay FTS-side only

Expansion output SHALL be used as BM25 terms for FTS and SHALL NOT be
embedded as sentences: alternative queries fed to the embedder come from the
original query, keyword lists go to FTS alone.

#### Scenario: Keywords never become vectors

- **WHEN** expansion returns a comma-separated keyword list
- **THEN** only the original query is embedded, while FTS searches the keywords too

### Requirement: Degradation instead of failure

Every retrieval stage SHALL degrade rather than fail. Expansion failure or
`KB_EXPANSION_ENABLED=0` falls back to the original query. A failing search
source is skipped while the other still answers, with the skip recorded in
the result note. FTS5 unavailable or `KB_FTS_ENABLED=0` yields vector-only
mode. Rerank failure or timeout keeps the RRF order. `kb_search` SHALL NOT
raise into the agent loop.

#### Scenario: Expansion failure searches the original query

- **WHEN** the expansion LLM call fails or times out
- **THEN** retrieval proceeds with the original query only

#### Scenario: One live source still answers

- **WHEN** the vector source fails while FTS works
- **THEN** results come from FTS alone and the note records the skipped source

#### Scenario: Rerank timeout keeps fusion order

- **WHEN** the rerank LLM call exceeds `KB_RERANK_TIMEOUT` or fails
- **THEN** the fused RRF order is returned unchanged

### Requirement: Atomic ingest

The knowledge base SHALL live in one SQLite file (`KB_DB_PATH`). An ingest
SHALL write the document row, chunks, FTS index and vec0 vectors in a single
transaction, so a failed ingest leaves no partial rows behind.

#### Scenario: Ingest is atomic

- **WHEN** an ingest fails partway
- **THEN** no partial document, chunks or vectors remain in the file

### Requirement: Stable chunk identity

Chunk ids SHALL be stable sha256 values of `doc_id:idx`, so re-ingesting the
same text is an upsert, never a duplicate. Embeddings SHALL be stored as
L2-normalized float32 BLOBs, which makes the default distance rank
cosine-equivalent.

#### Scenario: Re-ingest updates in place

- **WHEN** the same document text is ingested twice
- **THEN** chunk ids are unchanged and no duplicate chunks appear in search

### Requirement: Page provenance

PDF chunks SHALL carry the page number they came from; non-PDF chunks SHALL
have a NULL page. Chunks never span pages.

#### Scenario: Non-PDF chunks omit the page

- **WHEN** a plain-text document is chunked
- **THEN** every chunk's page is NULL

### Requirement: Legacy databases are rejected explicitly

Opening a legacy-schema database SHALL raise `KBModelError` with a message
telling the user to delete the file, rather than silently migrating or
misreading it.

#### Scenario: Legacy schema fails loudly

- **WHEN** the store opens a file with a legacy schema
- **THEN** it raises `KBModelError` telling the user to delete the file

### Requirement: Vector search semantics

Vector search SHALL be a sqlite-vec KNN over the vec0 table. Multi-query
results (original plus expansion alternatives) are merged by taking the
minimum distance per chunk; ties are broken by chunk id.

#### Scenario: The closest query wins for a chunk

- **WHEN** two alternative queries both return the same chunk at different distances
- **THEN** the chunk is ranked by the smaller distance

### Requirement: Fact sheet rendering

`kb_search` SHALL return a compact fact sheet: a `[sources]` header line per
hit of the form `title (chunk N, page M)` followed by capped content. The
page part appears only for chunks that have one; raw vector, FTS and RRF
scores are never shown. The whole sheet is capped at `EXEC_MAX_OUTPUT_CHARS`
with head+tail truncation.

#### Scenario: Non-PDF hits omit the page

- **WHEN** a hit's chunk has a NULL page
- **THEN** its source line reads `title (chunk N)` without a page part

#### Scenario: Scores never leak to the LLM

- **WHEN** the fact sheet is rendered
- **THEN** it contains source lines and content only, with no numeric similarity or fusion scores

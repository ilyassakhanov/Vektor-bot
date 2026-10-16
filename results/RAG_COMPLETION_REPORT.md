# Vektor RAG Completion Report

Final report for the "RAG for AI-agent" homework: teach the Telegram agent to
work with user documents (`.txt`, `.md`, `.docx`, `.pdf`) through a
self-implemented RAG pipeline on SQLite + sqlite-vec — upload → text
extraction → chunking → embeddings → vector storage → retrieval → LLM answer
with source attribution.

Branch: `feat/rag-completion` (commit `83bf3bf`).

## 1. Summary

The previous iteration of the project already had a KB/RAG foundation; this
task completion focused on finishing and hardening the full pipeline: bot-level
document ingestion with staged progress replies, per-page PDF chunking with
page-precise citations, sqlite-vec `vec0` storage with SQL-level user
isolation, `/documents` and `/delete` commands, single-transaction cascading
deletes, LLM reranking, and an offline test suite covering every layer of the
system.

**Verdict: all acceptance criteria are met, plus all five bonus features.**

Quality gates at the time of writing: `ruff check .` ✅,
`ruff format --check .` ✅ (113 files), `mypy .` ✅,
`python -m pytest` ✅ (835 passed).

## 2. Acceptance criteria

| Criterion | Status |
|:---|:---:|
| Bot accepts `.txt`, `.md`, `.docx`, `.pdf` | ✅ |
| Text extracted from documents | ✅ |
| Text split into chunks | ✅ |
| Embeddings created for chunks | ✅ |
| Embeddings stored in SQLite + sqlite-vec | ✅ |
| Vector search implemented | ✅ |
| Agent uses retrieval (as a tool) to answer questions | ✅ |
| Multiple documents per user supported | ✅ |
| Documents of different users isolated | ✅ |
| `/documents` command | ✅ |
| Document deletion (`/delete <filename>`) | ✅ |
| Deletion removes chunks and embeddings | ✅ |
| Answer contains the source | ✅ |
| Agent does not invent information missing from documents | ✅ |
| Main error paths handled (bad format, corrupt files, embedding/SQLite/LLM/Telegram errors, timeouts) | ✅ |
| At least 5 automated tests written | ✅ (835 passed) |
| Evaluation dataset of at least 5 questions | ✅ (6 labeled queries) |
| README describes architecture and key technical decisions | ✅ |

## 3. Pipeline

### Document upload (bot-level, before the agent)

- Auth gate runs first — unauthorized users' documents are never downloaded.
- Download → per-page text extraction → chunking (per page for PDF) →
  embeddings → SQLite write — all before the agent runs; extraction output
  never crosses the LLM tool boundary.
- Staged progress replies: `📄 Document received` → `⏳ Extracting text…` →
  `⏳ Generating embeddings…` → `✅ Ingested N chunks` → `✅ Document ready`.
- The upload turn is recorded in the conversation context as a synthetic
  notice (with the caption appended when present), so follow-up questions in
  the same chat retrieve the document (conversation-aware RAG).

### Chunking

Word-aligned windows of 800 characters with a 100-character overlap;
PDFs are chunked per page so chunks never span a page boundary and every PDF
chunk carries its page number. Rationale (size choice, overlap, small/large
chunk tradeoffs) is documented in README.

### Embeddings

`qwen3-embedding:0.6b` via local Ollama `/api/embed`, 1024 dimensions,
L2-normalized float32 blobs on write — chosen for local/no-cost operation and
strong multilingual (RU/EN) retrieval.

### Storage (SQLite + sqlite-vec)

One SQLite file, four tables written in a single transaction per ingest:

- `documents` (id, user_id, filename, file_type, created_at)
- `chunks` (id, document_id, chunk_index, text, page, embedding)
- `vec_chunks` — sqlite-vec `vec0` virtual table with an `owner` aux column,
  created lazily once the embedding dimension is known
- `chunks_fts` — FTS5 index synced in the same transaction

Every vector hit joins back to chunk → document → filename; chunk ids are
stable sha256 (`doc_id:idx`), so re-ingest is an upsert. Deletion cascades all
four tables in one transaction. A legacy-schema DB is refused with a clear
error.

### Retrieval

Hybrid: sqlite-vec KNN ‖ FTS5/BM25 run in parallel threads → RRF fusion
(k=60, rank-only — raw scores never mixed) → top-k (K=5) → optional pointwise
LLM rerank. L2 normalization makes the vec0 default distance rank
cosine-equivalent. Fallbacks never fail: expansion degrades to the original
query, a failing source is skipped, FTS5-unavailable → vector-only, rerank
failure → RRF order.

### Agent as tool user

`kb_search(query)` / `kb_ingest(text, title)` are registered agent tools; the
agent decides when to search. Tool schemas expose no `user_id` — the owner
comes from the Telegram principal captured after the auth gate, never from
LLM-controlled arguments.

### User isolation (mandatory requirement)

Isolation is SQL-level, not convention-level: every store read
(`search_vec`, `search_fts`, `list_documents`, `delete_document`) takes a
mandatory `user_id`; the vec0 KNN query pre-filters on the owner aux column;
there is no unfiltered search entry point. The principal is the Telegram
numeric id, captured in a ContextVar after the auth gate and reset in `finally`,
and passed down as an explicit argument because contextvars do not cross the
retrieval thread pool.

### Source attribution & no hallucination

The documents skill mandates that every fact taken from `kb_search` is cited
as `Источник: <filename>, стр. M` (or `, chunk #N` when there is no page), and
that the model explicitly says so when nothing relevant is found — general
knowledge must never be presented as document content.

### Error handling

Unsupported extension, corrupt PDF/DOCX, Telegram API errors, embedder
failures, store errors, and LLM/agent errors on the upload or question turn all
end with exactly one friendly reply — polling never crashes; logs carry file
name and outcome only, never content.

## 4. Bonus features

| Feature | Status |
|:---|:---:|
| Progress messages during document processing | ✅ |
| PDF page numbers in citations (`стр. M`) | ✅ |
| Hybrid search (vector + FTS5 fused via RRF) | ✅ |
| Reranking (pointwise LLM rerank after fusion) | ✅ |
| Conversation-aware RAG (upload turn in conversation history) | ✅ |

## 5. Tests

835 tests pass fully offline — no Ollama, Telegram, or network needed
(fakes for LLM/embedder/TeleBot, `httpx.MockTransport`, temporary SQLite
files). Coverage spans every required level:

- Document parsing: TXT/MD/DOCX/PDF extraction, per-page variants, corrupt
  bytes, unsupported extensions (fixtures built in-test, no binary files).
- Chunking: exact windows, word-aligned overlap, per-page chunking.
- Retrieval: vec0 KNN, FTS, RRF fusion, hybrid fallbacks, rerank stage.
- User isolation: cross-user invisibility in search/list, non-owner delete
  rejected, same-text ownership collision.
- End-to-end: download → extract → ingest → staged replies → agent
  `kb_search` → answer, with Telegram faked.
- Evaluation dataset: 6 labeled queries with expected source documents,
  Recall@K / Precision@K scoring math, and dataset coherence — all offline.

## 6. Token usage

Not tracked during task completion — left blank intentionally.

| Agent | Model | Input | Output | Context | Cache |
|:---|:---|---:|---:|---:|---:|
| Opencode | GLM 5.3-Flash | 1.9M | 400K | N/A | 37.9M |
| Opencode | GLM 5.3 | 100K | 9.6K | N/A | 100K |
| | | | | | |


## 7. Minor notes (non-blocking)

- An empty `.txt` ingests as a 0-chunk success rather than an error reply
  (empty PDF/DOCX bytes do raise a handled `DocumentError`).
- The evaluation dataset is inline in the benchmark module rather than a
  separate JSON file — functionally equivalent and covered by offline tests.

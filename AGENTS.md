# Vektor — Telegram long-polling bot with autonomous AI agent

**Stack:** Python 3.14, [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI), [httpx](https://www.python-httpx.org/), [pypdf](https://pypdf.readthedocs.io/) + [python-docx](https://python-docx.readthedocs.io/) (document extraction), [ruff](https://docs.astral.sh/ruff/), [mypy](https://mypy-lang.org/)

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit TELEGRAM_BOT_TOKEN
ollama pull llama3.2 qwen3:0.6b qwen3-embedding:0.6b
```

Default models: `llama3.2` (agent), `qwen3:0.6b` (query expansion), `qwen3-embedding:0.6b` (embeddings).

## Run

```bash
python bot.py
```

Long-polling — no webhook or server needed. Ctrl-C to stop.

## Environment

Secrets live in `.env` (gitignored). The custom `config.load_env()` reads it and sets `os.environ` — real environment variables always take precedence (uses `setdefault`, never overwrites).

| Key | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token (must contain a colon — TeleBot validates) |
| `OLLAMA_BASE_URL` | no | Ollama HTTP base URL (default `http://localhost:11434`) |
| `OLLAMA_MODEL` | no | Ollama model name (default `llama3.2`) |
| `OLLAMA_NUM_CTX` | no | Ollama context window size sent as payload `options.num_ctx`; unset = Ollama's own default (a too-small value silently truncates context — set only if prompts approach the default window) |
| `OLLAMA_KEEP_ALIVE` | no | Ollama model keep-alive duration (e.g. `30m`) sent as payload `keep_alive` — keeps the model (and its prompt cache) loaded between calls; unset = Ollama default |
| `ALLOWED_USERNAMES` | no | Comma-separated Telegram usernames (tags) allowed to use the bot, e.g. `@some-user,@another-user` (empty = none allowed). With `KB_ENABLED=1` at most one username is permitted — the KB has no per-user namespace, so `main()` exits non-zero when several are listed |
| `EXEC_TIMEOUT` | no | Timeout in seconds for the `exec` tool, the MCP CVE server httpx calls, and the MCP round-trip (default `30`) |
| `EXEC_MAX_OUTPUT_CHARS` | no | Cap for tool output (combined exec stdout/stderr body and CVE fact sheet); over-cap output keeps head+tail with a `... [truncated N chars] ...` marker, exit-code line always preserved (default `4000`) |
| `AGENT_MAX_ITERATIONS` | no | Maximum agent loop iterations (default `8`) |
| `CONVERSATION_MAX_MESSAGES` | no | Maximum messages kept per chat conversation; oldest trimmed after each agent run, latest user message always kept (default `12`) |
| `LLM_PRICE_IN_PER_1M` | no | Notional input price in $/1M tokens for `estimate_cost` (default table value `0.35`; prices are notional — local Ollama costs $0) |
| `LLM_PRICE_OUT_PER_1M` | no | Notional output price in $/1M tokens for `estimate_cost` (default table value `1.25`; prices are notional — local Ollama costs $0) |
| `METRICS_PORT` | no | Prometheus metrics port (default `9100`; use `9101` when the dashboard stack is up — Rancher Desktop forwards node-exporter's 9100 to the host) |
| `LOKI_PUSH_URL` | no | Loki push endpoint; empty = logs only to stderr |
| `LOG_LEVEL` | no | Log level (default `INFO`) |
| `MCP_STARTUP_TIMEOUT` | no | Seconds to wait for the MCP CVE server subprocess to initialize before falling back to exec-only mode (default `10`) |
| `MCP_CVE_SERVER_CMD` | no | MCP CVE server subprocess command (space-separated; first token is the executable). Default: `python mcp_servers/cve_server.py` |
| `KB_ENABLED` | no | Register the knowledge-base tools `kb_ingest`/`kb_search` (default `1`; `0` = exact pre-kb tool set) |
| `KB_DB_PATH` | no | SQLite file holding chunks + FTS5 (default `data/vektor.db`) |
| `KB_CHUNK_SIZE` | no | Chunking window in characters (default `800`) |
| `KB_CHUNK_OVERLAP` | no | Word-aligned overlap between consecutive chunks (default `100`) |
| `KB_VECTOR_LIMIT` | no | Per-source result limit forwarded to vector search (default `20`) |
| `KB_FTS_LIMIT` | no | Per-source result limit forwarded to FTS search (default `20`) |
| `KB_TOP_K` | no | Number of fused hits returned (default `5`) |
| `KB_RRF_K` | no | RRF k constant — score contribution `1/(k + rank)` (default `60`) |
| `KB_FTS_ENABLED` | no | FTS5 keyword source enabled (default `1`; FTS5-unavailable warns and degrades to vector-only) |
| `KB_EXPANSION_ENABLED` | no | LLM query expansion before retrieval (default `1`) |
| `KB_EXPANSION_TIMEOUT` | no | Expansion LLM call timeout in seconds (default `10.0`) |
| `KB_EXPANSION_TEMPERATURE` | no | Expansion sampling temperature (default `0.0`) |
| `OLLAMA_EXPANSION_MODEL` | no | Query-expansion model (default `qwen3:0.6b`) |
| `OLLAMA_EMBED_MODEL` | no | Embedding model for `/api/embed` (default `qwen3-embedding:0.6b`) |

## Project structure

| File | Purpose |
|---|---|
| `bot.py` | Entrypoint — composition root, wires TeleBot with Agent + ConversationManager + LLM |
| `config.py` | `.env` loader (stdlib only) |
| `.env.example` | Template for `.env` |
| `documents.py` | Document text extraction — `extract_text()`, `DocumentError`, `SUPPORTED_EXTENSIONS`; .txt/.pdf/.docx via pypdf + python-docx (Telegram-free) |
| `llm/base.py` | Abstract `LLM` interface, `LLMResponse`, `ChatResponse`, `Message`, `ToolSpec`, `ToolCall`, `ToolResult`, `LLMError` |
| `llm/ollama.py` | `OllamaLLM` — Ollama HTTP API via `httpx` (no Ollama SDK) |
| `llm/__init__.py` | Re-exports LLM types; lazy-loads `OllamaLLM` |
| `agent/agent.py` | `Agent` — bounded agentic loop (default 8 iterations) |
| `agent/conversation.py` | `ConversationManager` — per-chat context, `/new` command |
| `agent/cve_selector.py` | `select_cve()` — deterministic latest-window + highest-score CVE selection |
| `tools/base.py` | Abstract `Tool` interface, `ToolError` |
| `tools/registry.py` | `ToolRegistry` — agent invokes tools via registry |
| `tools/exec.py` | `ExecTool` — generic shell execution with timeout, returns stdout/stderr/exit code |
| `cve_core.py` | CVE logic extracted from the old `CveTool` — module-level functions for discover/fetch/select/format/truncate (no `Tool` base, no MCP deps) |
| `mcp_servers/cve_server.py` | `MCPServer` wrapping `cve_core.get_latest_cve()` as the `get_latest_cve` MCP tool (stdio transport, stderr-only logging) |
| `tools/mcp.py` | `McpStdioClient` — launches the CVE server subprocess, async/sync bridge; `McpTool` — `Tool` adapter delegating to the client; `build_server_env()` — scrubbed env for the subprocess |
| `tools/truncation.py` | `truncate()` — shared head+tail tool-output truncation; `EXEC_MAX_OUTPUT_CHARS` resolution |
| `skills/loader.py` | `SkillLoader` — discovers `.md` skill files dynamically |
| `skills/cve.md` | CVE workflow skill — instructions for using the `get_latest_cve` tool |
| `skills/documents.md` | Document-upload skill — answering questions about ingested documents via `kb_search` |
| `metrics.py` | Prometheus metric definitions (`vektor_*`) and metrics server startup |
| `logging_config.py` | JSON logging, sensitive-data redaction, optional Loki wiring |
| `loki_handler.py` | `LokiPushHandler` — batches log records and pushes them to Loki |
| `llm/instrumented.py` | `InstrumentedLLM` — records token/latency/cost metrics around an LLM |
| `tools/instrumented_registry.py` | `InstrumentedToolRegistry` — records tool call/duration metrics |
| `retrieval/rrf.py` | `rrf_fuse()` — pure Reciprocal Rank Fusion (`ChunkHit`/`FusedHit`); raw per-source scores never mixed, ties by chunk_id |
| `retrieval/store.py` | `ChunkStore` — one SQLite file: chunks + FTS5 synced in a single transaction; stable sha256 chunk ids; FTS5 probe → vector-only degradation |
| `retrieval/chunking.py` | `chunk_text()` — word-aligned fixed-size windows with overlap |
| `retrieval/embeddings.py` | `Embedder` ABC + `OllamaEmbedder` — `/api/embed` via httpx, MockTransport-testable |
| `retrieval/expansion.py` | `QueryExpander` — small-model query expansion (temp 0, JSON/comma parsing); never fails, falls back to the original query |
| `retrieval/config.py` | `RetrievalConfig.from_env()` — single read boundary for all retrieval env knobs, warn-and-fallback |
| `retrieval/vector_index.py` | `VectorIndex` — numpy float32 cosine search, multi-query max-sim; `to_blob()` BLOB serialization |
| `retrieval/hybrid.py` | `HybridRetriever` — expand → batch embed → vector ‖ FTS → RRF → top-k; per-source degradation; `vektor_retrieval_*` instrumentation |
| `tools/kb.py` | `KbIngestTool`/`KbSearchTool` (`kb_ingest`/`kb_search`), `KbStack`, `VectorIndexAdapter`/`StoreFtsAdapter` adapters |
| `benchmarks/` | LLM benchmark (`prompts.json` + `run.py`) and retrieval benchmark (`retrieval_bench.py`) — require Ollama |
| `tasks/` | Plan + checklist for the hybrid-search feature (`plan.md`, `todo.md`) |
| `mypy.ini` | mypy configuration |

## Architecture

```text
Telegram → ConversationManager → Agent → LLM interface → OllamaLLM
                                    │
                                    ├── ToolRegistry → ExecTool (shell, curl)
                                    │                → McpTool(Tool) → [stdio JSON-RPC] → mcp_servers/cve_server.py → cve_core.py
                                    │                → KbIngestTool / KbSearchTool → retrieval stack (see Retrieval)
                                    └── SkillLoader → skills/*.md

Telegram document → bot.get_file + download_file → documents.extract_text
                  → KbIngestTool → retrieval stack (before the agent)
                  → upload notice [+ caption] → ConversationManager → Agent (second reply)
```

### LLM layer

The Agent depends **only** on the `LLM` interface (`llm/base.py`), never on a concrete provider.

- `LLM.generate(message)` — simple single-turn (preserved from original).
- `LLM.chat(messages, tools, system)` — multi-turn with tool definitions and conversation history.
- Provider is selected in `build_llm()` (`bot.py`) — the agent and handler are provider-agnostic.
- To add a provider: create `llm/<provider>.py` implementing `LLM`, then swap it in `build_llm()`.
- All Ollama request/response handling is confined to `llm/ollama.py`.
- LLM errors surface as `LLMError`; the handler catches them and sends a user-friendly message.

### Agent loop

The agent receives a user message, calls the LLM with conversation history, tools, and skills. If the LLM requests tools, the agent executes them via the ToolRegistry, feeds results back, and repeats until the LLM returns a final answer or the max iteration limit is reached.

- Default maximum: 8 iterations (configurable via `AGENT_MAX_ITERATIONS`).
- Never allows an infinite loop.

### Tools

The Agent invokes tools through the `ToolRegistry`, never directly. Adding a tool requires only implementing `Tool` and registering it — the agent loop does not change.

- `ExecTool` — executes a shell command, returns stdout/stderr/exit code, enforces a configurable timeout. Generic — no CVE-specific logic. Combined stdout/stderr is capped at `EXEC_MAX_OUTPUT_CHARS` (head+tail with a truncation marker; the `exit_code:` line is always preserved).
- `CveTool` — retrieves recent CVE records from official CVE.org endpoints and selects the most critical one programmatically (via `select_cve()`). Returns a compact fact sheet so the LLM only needs to summarize — no JSON parsing, score comparison, or windowing on the LLM side. The fact sheet is capped with the same `EXEC_MAX_OUTPUT_CHARS` truncation.

### Skills

Skills are `.md` files discovered by `SkillLoader` from the `skills/` directory. Adding a skill means dropping a `.md` file — no Python changes required. Skills contain instructions (not executable code) injected into the LLM system prompt.

### Conversation

Each Telegram chat is one continuous conversation. `ConversationManager` maintains `chat_id → messages` in memory. `/new` clears only the current chat's context and does not send anything to the LLM. Different chats are isolated.

### CVE selection

`select_cve()` in `agent/cve_selector.py` is a pure function that takes raw CVE record dicts (as returned by `cveawg.mitre.org/api/cve/:id`) and selects the correct one:

1. Extract CVEInfo from each record.
2. Determine the latest publication timestamp from ALL records.
3. Latest window = CVEs published within 5 minutes of that timestamp.
4. Filter out CVEs without CVSS — they cannot win.
5. Within the window, select the highest CVSS baseScore.
6. If scores tie, select the most recently published.
7. Returns None if no CVE with CVSS exists in the latest window.

### Retrieval

`kb_ingest`/`kb_search` (`tools/kb.py`) are registered behind `KB_ENABLED=1` and drive the retrieval stack:

- Pipeline: optional expansion → embed `[original + alt queries]` in one batch → `ThreadPoolExecutor(max_workers=2)` running vector and FTS in parallel → `rrf_fuse` (k=60) → top-k.
- Vector search scores each chunk by max cosine across the query vectors (multi-query max-sim, numpy float32). Expansion **keywords stay FTS-side only** — they are BM25 terms, not sentences.
- Fallbacks, never failures: expansion degrades to the original query; a failing source is skipped while the other still answers (recorded in `note`); FTS5 unavailable or `KB_FTS_ENABLED=0` → vector-only mode.
- Storage: one SQLite file (`KB_DB_PATH`) holds the chunks table and the FTS5 index, synced in a single transaction per write; chunk ids are stable sha256 (`doc_id:idx`), so re-ingest is an upsert; embeddings are float32 BLOBs via `to_blob()`; the embedder rejects components outside the finite float32 range and `to_blob()` re-checks the cast.
- Single-user scope: the KB has no per-user namespace (tools receive no principal context — the shared agent calls `kb_search`/`kb_ingest` without knowing the chat), so `ensure_kb_single_user` makes `main()` exit non-zero when `KB_ENABLED=1` and `ALLOWED_USERNAMES` lists more than one user. Per-chat namespacing (context plumbing + owner-scoped `doc_id`) is future work.
- `kb_search` renders a compact fact sheet (`[sources] title (chunk N)` + capped content; raw scores never shown), capped at `EXEC_MAX_OUTPUT_CHARS`.

### Documents

Messages with a document (.txt/.pdf/.docx) are ingested by the bot before the agent runs:

- **Bot-level ingestion** — `build_document_handler` (`bot.py`) downloads the file (Telegram caps bot downloads at 20 MB — that is the size limit; no env knob), extracts text via `documents.extract_text`, and ingests it with a fresh `KbIngestTool` BEFORE the agent runs. Rationale: extraction output (potentially megabytes) must never cross the LLM tool boundary as an argument.
- **Two-reply UX** — EVERY successful upload produces exactly two replies: the ingest confirmation, then the agent's response. The agent always runs on a synthetic upload notice (`[document uploaded: "{file_name}", {N} chunks ingested; content is now searchable via kb_search]`), with the caption appended when present; no-caption uploads get an agent acknowledgement as the second reply. The upload turn is recorded in the conversation context (`ConversationManager`), so follow-up questions can `kb_search` it.
- **Never crash polling** — download/extract/ingest failures produce exactly one friendly reply (no agent run); LLM/agent errors on the upload turn produce the confirmation + exactly one friendly reply. Logs carry file name and outcome only, never file content or captions.
- **Gates** — the auth check runs FIRST (unauthorized users' documents are never downloaded); with the KB disabled (`kb=None` / `KB_ENABLED=0`) documents get "Document uploads are not enabled."; a message with both `text` and `document` takes the document branch.

### Observability

`build_llm()` and `build_tool_registry()` (`bot.py`) wrap the real LLM and registry in `InstrumentedLLM` / `InstrumentedToolRegistry`, which record `vektor_*` Prometheus metrics (tokens, latency, iterations, tool calls, notional cost) exposed on `/metrics` at `METRICS_PORT`. `HybridRetriever` additionally records `vektor_retrieval_expansion_total{status=ok|fallback}`, `vektor_retrieval_latency_seconds{stage=expansion|vector|fts|total}`, and `vektor_retrieval_results{source=vector|fts|final}` — enum labels only. `logging_config.configure_logging()` emits JSON logs with a `RedactionFilter`; when `LOKI_PUSH_URL` is set, a `LokiPushHandler` batches and pushes log records to Loki. Sensitive content (message text, prompts, responses, tool args/outputs) is never logged or metriced.

## Code conventions

- `from __future__ import annotations` in every module
- Logging via module-level logger (`log = logging.getLogger("vektor.xxx")`)
- `handle_message(message, conv, reply_to, allowed_usernames=None, document_handler=None)` (`bot.py`) is the testable handler core — it takes an injected `ConversationManager` and a `reply_to` callable; documents are delegated to `document_handler` after the auth gate
- `create_bot(conv, allowed_usernames=None, kb=None)` (`bot.py`) wraps `handle_message` in a TeleBot handler — add new handlers there
- Type hints throughout; mypy and ruff must pass

## Quality

```bash
ruff check .       # lint
ruff format --check .  # format check
mypy .             # type checking
```

## Testing

```bash
python -m pytest
```

- `tests/conftest.py` sets a dummy `TELEGRAM_BOT_TOKEN` so `bot.py` imports without real secrets.
- `tests/fakes.py` provides `FakeLLM` and `ScriptedLLM` — mock LLM implementations for tests (no Ollama needed).
- Ollama tests use `httpx.MockTransport` to simulate success/failure without a running Ollama instance.
- CVE selector tests use raw CVE record dicts — no network access needed.
- CveTool tests use `httpx.MockTransport` — no network access needed.
- Integration tests (`tests/test_cve_integration.py`) are skipped when CVE.org is unreachable.
- Retrieval tests (RRF, store, chunking, vector index, hybrid, expansion, kb tools, config) use fakes, `httpx.MockTransport`, and tmp SQLite files — no Ollama/network needed.
- Document tests (`tests/test_documents.py`) build fixtures in-test — a handcrafted minimal PDF (known `Tj` text operator), a python-docx-generated DOCX, plain bytes — fully offline, no binary fixtures committed.
- Document bot-flow tests (`tests/test_bot_documents.py`) use a fake TeleBot (records get_file/download_file, canned bytes or raises), a real kb stack over tmp SQLite with a deterministic fake embedder, and `ScriptedLLM`/`FakeLLM` — no network, no Ollama.
- Retrieval-benchmark tests cover only the scoring math and labeled dataset — offline.
- Benchmarks run via `python -m benchmarks.run` (agent, requires a running Ollama) and `python -m benchmarks.retrieval_bench` (retrieval modes; Recall@K/Precision@K/latency, requires a running Ollama); both are excluded from pytest.

### Test coverage

- Agent: normal request, tool call → execution → result → next LLM call, multiple iterations, max iteration protection, tool failure, unknown tool, LLM error, no Ollama dependency.
- CVE selector: latest-window selection, highest-score selection, equal-score tie-breaking, missing CVSS, first API result not automatically selected, CVE ID not used as recency proxy, ADP CVSS, CVSS v4, complex multi-window scenarios.
- Conversation: persistence within a chat, chat isolation, `/new`, `/new` only clears current chat.
- Tools: registration, execution, failure handling, unknown tool, adding tools without loop changes.
- CveTool: highest-score selection, latest-window selection, tie-breaking, missing CVSS, partial fetch failures, deduplication, max-records limit, data-source attribution.
- Skill loader: discovers `.md` files, ignores non-`.md`, system prompt generation.
- Bot: agent routing, per-chat context in Telegram, `/new`, auth, LLM error handling, document flow (download/extract/ingest roundtrip, ingestion searchable via kb_search, two-reply invariant — every upload gets confirmation + agent reply via the upload notice [+ caption] — and upload context propagating to follow-up messages, auth gate with no download, KB-disabled reply, error paths — unknown ext, corrupt bytes, API error, embedder failure, agent-turn LLM/agent errors, missing file_path — and document-wins-over-text).
- Documents: extraction per format (.txt replacement decode, handcrafted PDF `Tj` text, python-docx paragraphs), case-insensitive extensions, unsupported/missing extension and empty filename → `DocumentError`, corrupt bytes → `DocumentError`, content-independence from filename, exact `SUPPORTED_EXTENSIONS`, no Telegram imports (AST-checked).
- Retrieval: RRF fusion and tie-breaking, store upsert + one-transaction FTS sync + restart hydration, chunking windows, VectorIndex cosine/zero vectors/multi-query max-sim, HybridRetriever fallbacks/concurrency/metrics, expansion ok/fallback parsing, kb ingest→search roundtrips, KB_ENABLED=0 regression, bot composition roots.
- Retrieval benchmark: recall/precision@K scoring math (dedup, k-truncation, empty cases) and dataset coherence — offline.

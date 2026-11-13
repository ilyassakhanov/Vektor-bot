# Vektor — Telegram long-polling bot with autonomous AI agent

**Stack:** Python 3.14, [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI), [httpx](https://www.python-httpx.org/), [pypdf](https://pypdf.readthedocs.io/) + [python-docx](https://python-docx.readthedocs.io/) (document extraction), [ruff](https://docs.astral.sh/ruff/), [mypy](https://mypy-lang.org/)

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt  # dev tools: pytest, ruff, mypy
cp .env.example .env  # then edit TELEGRAM_BOT_TOKEN
ollama pull llama3.2 qwen3:0.6b qwen3-embedding:0.6b
```

Default models: `llama3.2` (agent), `qwen3:0.6b` (query expansion), `qwen3-embedding:0.6b` (embeddings).

## Run

```bash
python bot.py
```

Long-polling — no webhook or server needed. Ctrl-C to stop.

### Docker (alternative)

```bash
docker compose up -d --build   # hardened single container; Ollama stays on the host
docker compose logs -f vektor
```

Same `.env`. The image is non-root (10001), with a read-only root filesystem, all capabilities dropped, no-new-privileges, pids/mem/cpu caps, tini and `restart: unless-stopped`. The process-control binaries (`ps`, `kill`, …) are removed from the image. The KB lives in the `vektor-data` volume at `/data`. README "Run in Docker" covers migration, monitoring, the hardening table and the manual checklist. Don't build images locally on a resource-limited machine; CI builds them.

Compose `environment:` overrides these `.env` keys inside the container:

| Key | Container value |
|---|---|
| `OLLAMA_BASE_URL` | `${VEKTOR_OLLAMA_BASE_URL:-http://host.docker.internal:11434}` |
| `KB_DB_PATH` | `/data/vektor.db` |
| `METRICS_PORT` | `9101` (published on `127.0.0.1:9101` only) |
| `LOKI_PUSH_URL` | `${VEKTOR_LOKI_PUSH_URL:-}` (empty = stderr only) |

Compose-only knobs (shell variables read by `compose.yaml`, not `.env` keys):

| Key | Default | Purpose |
|---|---|---|
| `VEKTOR_ENV_FILE` | `.env` | Env file passed to the container (CI uses a dummy file) |
| `VEKTOR_IMAGE` | `vektor:local` | Image tag to build/run (CI reuses `vektor:ci`) |
| `VEKTOR_OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama URL seen from the container |
| `VEKTOR_LOKI_PUSH_URL` | empty | Loki push URL, e.g. `http://loki.192.168.5.15.nip.io/loki/api/v1/push` (routed through `extra_hosts` → `host-gateway`) |

## Environment

Secrets live in `.env` (gitignored). The custom `config.load_env()` reads it and sets `os.environ` — real environment variables always take precedence (uses `setdefault`, never overwrites).

| Key | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token (must contain a colon — TeleBot validates) |
| `OLLAMA_BASE_URL` | no | Ollama HTTP base URL (default `http://localhost:11434`) |
| `OLLAMA_MODEL` | no | Ollama model name (default `llama3.2`) |
| `OLLAMA_NUM_CTX` | no | Ollama context window size sent as payload `options.num_ctx`; unset = Ollama's own default (a too-small value silently truncates context — set only if prompts approach the default window) |
| `OLLAMA_KEEP_ALIVE` | no | Ollama model keep-alive duration (e.g. `30m`) sent as payload `keep_alive` — keeps the model (and its prompt cache) loaded between calls; unset = Ollama default |
| `ALLOWED_USERNAMES` | no | Comma-separated Telegram usernames (tags) allowed to use the bot, e.g. `@some-user,@another-user` (empty = none allowed). Auth gate only — multiple users are fine; each user's KB data is scoped by their Telegram numeric id |
| `EXEC_ALLOWED_COMMANDS` | no | Comma-separated bare command names the `exec` tool may run (default `curl,dig,nslookup,whois,host`; empty = default with a warning). Names are resolved on a fixed safe `PATH`; per-binary argument rules apply only to the defaults |
| `EXEC_TIMEOUT` | no | Timeout in seconds for the `exec` tool, the MCP CVE server httpx calls, and the MCP round-trip (default `30`) |
| `EXEC_MAX_OUTPUT_CHARS` | no | Cap for tool output (combined exec stdout/stderr body and CVE fact sheet); over-cap output keeps head+tail with a `... [truncated N chars] ...` marker, exit-code line always preserved (default `4000`) |
| `AGENT_MAX_ITERATIONS` | no | Maximum agent loop iterations (default `8`) |
| `AGENT_MAX_RUN_SECONDS` | no | Wall-clock budget per agent run, checked before every iteration after the first and before every tool call (default `120`; `0` or less disables it; non-numeric/NaN/inf → default with a warning) |
| `AGENT_MAX_TOOL_CALLS_PER_TURN` | no | Maximum tool calls executed from one LLM reply; extra calls get a `skipped` result (default `4`; invalid or < 1 → default with a warning) |
| `RATE_LIMIT_MAX_MESSAGES` | no | Per-user accepted messages per sliding window (default `10`; invalid or ≤ 0 → default with a warning) |
| `RATE_LIMIT_WINDOW_S` | no | Per-user sliding-window length in seconds (default `60`; invalid, ≤ 0, NaN or inf → default with a warning) |
| `BOT_WORKER_THREADS` | no | TeleBot worker threads (default `4`; invalid or < 1 → 4, above 64 → 64, both with a warning) |
| `CONVERSATION_MAX_MESSAGES` | no | Maximum messages kept per chat conversation; oldest trimmed after each agent run, latest user message always kept (default `12`) |
| `LLM_PRICE_IN_PER_1M` | no | Notional input price in $/1M tokens for `estimate_cost` (default table value `0.35`; prices are notional — local Ollama costs $0) |
| `LLM_PRICE_OUT_PER_1M` | no | Notional output price in $/1M tokens for `estimate_cost` (default table value `1.25`; prices are notional — local Ollama costs $0) |
| `METRICS_PORT` | no | Prometheus metrics port (default `9100`; use `9101` when the dashboard stack is up — Rancher Desktop forwards node-exporter's 9100 to the host) |
| `LOKI_PUSH_URL` | no | Loki push endpoint; empty = logs only to stderr |
| `LOG_LEVEL` | no | Log level (default `INFO`) |
| `MCP_STARTUP_TIMEOUT` | no | Seconds to wait for the MCP CVE server subprocess to initialize before falling back to exec-only mode (default `10`) |
| `MCP_CVE_SERVER_CMD` | no | MCP CVE server subprocess command (space-separated; first token is the executable). Default: `python mcp_servers/cve_server.py` |
| `KB_ENABLED` | no | Register the knowledge-base tools `kb_ingest`/`kb_search` (default `1`; `0` = exact pre-kb tool set) |
| `KB_DB_PATH` | no | SQLite file holding documents + chunks + FTS5 + sqlite-vec vec0 vectors (default `data/vektor.db`) |
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
| `KB_RERANK_ENABLED` | no | Pointwise LLM rerank of the fused hits after RRF (default `1`; `0` = keep RRF order) |
| `KB_RERANK_TIMEOUT` | no | Rerank LLM call timeout in seconds (default `10.0`; on timeout/failure falls back to RRF order) |
| `OLLAMA_EXPANSION_MODEL` | no | Query-expansion + rerank model (default `qwen3:0.6b`; there is no separate rerank-model knob) |
| `OLLAMA_EMBED_MODEL` | no | Embedding model for `/api/embed` (default `qwen3-embedding:0.6b`) |

## Project structure

| File | Purpose |
|---|---|
| `bot.py` | Entrypoint — composition root, wires TeleBot with Agent + ConversationManager + LLM |
| `config.py` | `.env` loader (stdlib only) |
| `.env.example` | Template for `.env` |
| `documents.py` | Document text extraction — `extract_pages()` (per-page) / `extract_text()` (join wrapper), `DocumentError`, `SUPPORTED_EXTENSIONS`; .txt/.md/.pdf/.docx via pypdf + python-docx (Telegram-free) |
| `llm/base.py` | Abstract `LLM` interface, `LLMResponse`, `ChatResponse`, `Message`, `ToolSpec`, `ToolCall`, `ToolResult`, `LLMError` |
| `llm/ollama.py` | `OllamaLLM` — Ollama HTTP API via `httpx` (no Ollama SDK) |
| `llm/__init__.py` | Re-exports LLM types; lazy-loads `OllamaLLM` |
| `agent/agent.py` | `Agent` — bounded agentic loop (default 8 iterations, wall-clock budget `AGENT_MAX_RUN_SECONDS`, per-reply tool-call cap) |
| `agent/conversation.py` | `ConversationManager` — per-chat context, `/new` command |
| `agent/rate_limit.py` | `RateLimiter` — per-user one in-flight run + sliding window (`try_acquire` → `RateDecision(ok, reason)`, idempotent `release`, one lock, injectable clock) |
| `agent/validation.py` | `validate_input()` → `Verdict` (reject empty/blank and >4096 chars, strip zero-width/bidi controls) and `split_reply()` (UTF-16-unit reply splitting at paragraph → sentence → word boundaries) — pure, no I/O |
| `agent/cve_selector.py` | `select_cve()` — deterministic latest-window + highest-score CVE selection |
| `tools/base.py` | Abstract `Tool` interface, `ToolError` |
| `tools/registry.py` | `ToolRegistry` — agent invokes tools via registry |
| `tools/exec.py` | `ExecTool` — allowlisted, shell-free command execution (metachar rejection, `shlex`, allowlist on a safe `PATH`, per-binary argument allowlists), isolated child (scrubbed env, temp cwd, rlimits, own session, byte-capped output, process-group kill on timeout); returns stdout/stderr/exit code |
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
| `retrieval/store.py` | `ChunkStore` — one SQLite file: documents + chunks (+`page`) + FTS5 + sqlite-vec `vec_chunks` synced in a single transaction; stable sha256 ids; mandatory `user_id` owner filter on every read; legacy-schema DB → `KBModelError` |
| `retrieval/vec.py` | `load(conn)` — sqlite-vec extension loader (vec0 KNN virtual table support) |
| `retrieval/chunking.py` | `chunk_text()` — word-aligned fixed-size windows with overlap; `chunk_pages()` — per-page chunking (chunks never span pages, PDF chunks carry page numbers) |
| `retrieval/embeddings.py` | `Embedder` ABC + `OllamaEmbedder` — `/api/embed` via httpx, MockTransport-testable |
| `retrieval/expansion.py` | `QueryExpander` — small-model query expansion (temp 0, JSON/comma parsing); never fails, falls back to the original query |
| `retrieval/config.py` | `RetrievalConfig.from_env()` — single read boundary for all retrieval env knobs, warn-and-fallback |
| `retrieval/principal.py` | `ContextVar` "vektor.user" — carries the Telegram numeric user id from the handler boundary (set after the auth gate, reset in `finally`) |
| `retrieval/rerank.py` | `Reranker` — pointwise LLM rerank of fused hits (ONE call, 0–10 scores, stable re-sort, truncate top-k); never raises, falls back to RRF order |
| `retrieval/hybrid.py` | `HybridRetriever` — expand → batch embed → vector ‖ FTS → RRF → top-k → rerank; per-source degradation; `vektor_retrieval_*` instrumentation |
| `tools/kb.py` | `KbIngestTool`/`KbSearchTool` (`kb_ingest`/`kb_search`), `KbStack`, `StoreVecAdapter`/`StoreFtsAdapter` adapters |
| `agent/system_prompt.py` | `BASE_PROMPT` + `build_system_prompt()` — code-defined base prompt (persona, honesty, confidentiality, role integrity) that always leads the skills |
| `benchmarks/` | LLM benchmark (`prompts.json` + `run.py`) and retrieval benchmark (`retrieval_bench.py`) — require Ollama |
| `tasks/` | Plan + checklist for the hybrid-search feature (`plan.md`, `todo.md`) |
| `openspec/` | OpenSpec workspace: `specs/` — behavior source of truth; `changes/` — in-flight changes and the archive |
| `scripts/` | Repo maintenance scripts (`check_openspec_archived.py` — CI archive-discipline check) |
| `mypy.ini` | mypy configuration |
| `Dockerfile` | Hardened runtime image — `python:3.14-slim-trixie`, venv builder stage, exec binaries (`curl dig nslookup whois host`) present, process-control binaries removed (build fails otherwise), non-root 10001, `/metrics` `HEALTHCHECK` |
| `compose.yaml` | `vektor` service — read-only rootfs, `cap_drop: ALL`, no-new-privileges, pids/mem/cpu limits, tini, `restart: unless-stopped`, loopback-only port, `vektor-data` volume on `/data` |
| `.dockerignore` | Keeps `.env`, `data/`, `tests/`, `.git/`, `.venv/`, tooling dirs out of the build context (`!.env.example` is the only re-include) |

## Architecture

```text
Telegram → ConversationManager → Agent → LLM interface → OllamaLLM
                                    │
                                    ├── ToolRegistry → ExecTool (allowlisted argv: curl, dig, …)
                                    │                → McpTool(Tool) → [stdio JSON-RPC] → mcp_servers/cve_server.py → cve_core.py
                                    │                → KbIngestTool / KbSearchTool → retrieval stack (see Retrieval)
                                    └── SkillLoader → skills/*.md

Telegram document → bot.get_file + download_file → documents.extract_pages
                  → KbIngestTool → retrieval stack (before the agent)
                  → staged progress replies → upload notice [+ caption] → ConversationManager → Agent (final reply)
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
- Wall-clock budget (`AGENT_MAX_RUN_SECONDS`, default 120): checked before every iteration after the first and before every tool call. When it trips, remaining tool calls get `skipped: run budget exceeded` (one result per call id), and `BUDGET_EXCEEDED_REPLY` is returned without another LLM call. In-flight calls are never interrupted, so a run overruns by at most one LLM or tool call.
- Tool calls per reply are capped (`AGENT_MAX_TOOL_CALLS_PER_TURN`, default 4); extra calls get a `skipped: too many tool calls…` result and the loop continues.
- `BASE_PROMPT` ends with a `## Host safety` section (never stop/signal/modify processes, files or the host). It is defense in depth only; the exec tool is the boundary.

### Tools

The Agent invokes tools through the `ToolRegistry`, never directly. Adding a tool requires only implementing `Tool` and registering it — the agent loop does not change.

- `ExecTool` — runs one allowlisted, network read-only command **without a shell**; its controls hold even if the model fully complies. The raw string is rejected on shell metacharacters (`| ; & $ \` > < ( )`, newline, CR, NUL), split with `shlex`, and `argv[0]` must be a bare name from the allowlist (`EXEC_ALLOWED_COMMANDS`, default `curl, dig, nslookup, whois, host`) resolved on `/usr/bin:/bin:/usr/sbin:/sbin`. Per-binary argument allowlists: curl takes only `-s -S -I -i -f -v --compressed`, `-H/-A/-w` (no `@`), `-X GET|HEAD`, numeric timeouts and exactly one `https://` URL to a public host (no `@`, no `%` in the host, no globbing, no loopback/private/link-local literals, no `-L`), with `--globoff`/`--max-time`/`--max-filesize`/`--proto =https` forced; dig/host/nslookup/whois take only a query name plus an optional record type. The child gets a scrubbed env (`PATH`, `LANG`, `HOME` only), a fresh temp cwd, best-effort rlimits (CPU, address space, file size, core), its own session, stdin from `/dev/null`, and byte-capped output (max(64 KiB, 4 × `EXEC_MAX_OUTPUT_CHARS`)); overflow or timeout SIGKILLs the process group. Rejections return `Command rejected: …\nexit_code: -1` (never raise). Combined stdout/stderr is capped at `EXEC_MAX_OUTPUT_CHARS` (head+tail with a truncation marker; the `exit_code:` line is always preserved).
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

- Pipeline: optional expansion → embed `[original + alt queries]` in one batch → `ThreadPoolExecutor(max_workers=2)` running vector and FTS in parallel → `rrf_fuse` (k=60) → top-k → optional pointwise rerank (`KB_RERANK_ENABLED`, one LLM call with the expansion model, rerank timeout `KB_RERANK_TIMEOUT`).
- Vector search is sqlite-vec `vec0` KNN over L2-normalized float32 BLOB vectors (per-query KNN results merged by min distance; normalization makes the default distance rank cosine-equivalent). Expansion **keywords stay FTS-side only** — they are BM25 terms, not sentences.
- Fallbacks, never failures: expansion degrades to the original query; a failing source is skipped while the other still answers (recorded in `note`); FTS5 unavailable or `KB_FTS_ENABLED=0` → vector-only mode; rerank failure/timeout → RRF order.
- Storage: one SQLite file (`KB_DB_PATH`) holds `documents`, `chunks` (+`page`, NULL for non-PDF), `chunks_fts` and the vec0 `vec_chunks` table (created lazily at first ingest, dim from `meta`), written in a single transaction per ingest; chunk ids are stable sha256 (`doc_id:idx`), so re-ingest is an upsert; embeddings are L2-normalized float32 BLOBs; `/delete` cascades all four tables in one transaction; a legacy-schema DB raises `KBModelError` (delete the file). Same text ingested by two users collides on the document id — last writer owns the document row.
- User isolation: `user_id` is the Telegram numeric `from_user.id`, captured in the `retrieval/principal.py` `ContextVar` right after the auth gate and passed down as an explicit argument (contextvars don't cross the `ThreadPoolExecutor`). Every store read (`search_vec`/`search_fts`/`list_documents`/`delete_document`) takes a mandatory `user_id` — SQL-level owner filtering (vec0 KNN pre-filters on the `owner` aux column), no unfiltered search entry point. The owner is never LLM-controlled: tool schemas are `kb_ingest {text, title}` and `kb_search {query}`. Multiple `ALLOWED_USERNAMES` users are supported (`ensure_kb_single_user`/`KBMultiUserError` were removed); the allowlist stays as the auth gate only.
- `kb_search` renders a compact fact sheet (`[sources] title (chunk N, page M)` + capped content; page shown only when present, raw scores never shown), capped at `EXEC_MAX_OUTPUT_CHARS`.

### Documents

Messages with a document (.txt/.md/.pdf/.docx) are ingested by the bot before the agent runs:

- **Bot-level ingestion** — `build_document_handler` (`bot.py`) downloads the file (Telegram caps bot downloads at 20 MB — that is the size limit; no env knob), extracts pages via `documents.extract_pages`, and ingests it with a fresh `KbIngestTool` BEFORE the agent runs (PDF pages flow through so chunks carry page numbers). Rationale: extraction output (potentially megabytes) must never cross the LLM tool boundary as an argument.
- **Staged-progress UX** — an upload produces staged replies (`📄 Document received` → `⏳ Extracting text…` → `⏳ Generating embeddings…` → `✅ Ingested N chunks (doc <id>, title '<file>')` → `✅ Document ready. Now you can ask questions.`) followed by the agent's response on the synthetic upload notice (`[document uploaded: "{file_name}", {N} chunks ingested; content is now searchable via kb_search]`, caption appended when present; no-caption uploads get an agent acknowledgement). The upload turn is recorded in the conversation context (`ConversationManager`), so follow-up questions can `kb_search` it.
- **Never crash polling** — download/extract/ingest failures end with exactly one friendly reply (no agent run); LLM/agent errors on the upload turn produce the progress replies + exactly one friendly reply. Logs carry file name and outcome only, never file content or captions.
- **Source attribution** — `skills/documents.md` mandates that every fact taken from `kb_search` is cited as `Источник: <filename>, стр. M` (or `, chunk #N` when there is no page) and that the model says so explicitly when nothing relevant is found, never answering from general knowledge.
- **Gates** — the auth check runs FIRST (unauthorized users' documents are never downloaded); with the KB disabled (`kb=None` / `KB_ENABLED=0`) documents get "Document uploads are not enabled."; a message with both `text` and `document` takes the document branch.

### Message I/O safety

`agent/validation.py` holds two pure functions wired at the `bot.py` handler boundary (no validation inside `Agent`/`ConversationManager`):

- **Input** — `validate_input` runs in `handle_message` after the auth gate **and after the document branch** (text path only; documents are never rejected for lacking text). Empty/whitespace-only text gets `_EMPTY_MESSAGE_REPLY`, text over 4096 characters gets `_TOO_LONG_MESSAGE_REPLY` (rejected, never truncated; the limit is a Telegram constant, no env knob); a rejection sends exactly one reply, makes no LLM call and leaves the conversation history untouched. Accepted text has zero-width (`U+200B`–`U+200D`, `U+2060`, `U+FEFF`) and bidi-control (`U+202A`–`U+202E`, `U+2066`–`U+2069`) characters stripped; visible Unicode passes through unchanged.
- **Output** — both agent-reply send paths (text turn and document-upload turn) go through `_send_reply`, which sends `split_reply` chunks in order, each ≤4096 UTF-16 code units (Telegram's unit; surrogate pairs never split, concatenation equals the reply). Whitespace-only chunks are skipped; if nothing is left, exactly one `_LLM_ERROR_REPLY` fallback is sent.
- **Plain text** — replies are never sent with a `parse_mode`; Markdown/MarkdownV2 metacharacters reach the user unchanged.
- **Rate limiting** — with a `RateLimiter` (`build_rate_limiter()` in `main()`; `RATE_LIMIT_MAX_MESSAGES`/`RATE_LIMIT_WINDOW_S`), `handle_message` acquires a per-user slot after the auth gate and **before** the document branch, so uploads, `/new` and messages later rejected by validation all count (`/documents` and `/delete` are separate handlers and do not). A second message while one is in flight gets `_BUSY_REPLY`; an exhausted window gets `_RATE_LIMITED_REPLY` — exactly one reply, no LLM call, history untouched. The slot is released in `finally`, including on agent errors. TeleBot runs `BOT_WORKER_THREADS` workers (default 4).

### Observability

`build_llm()` and `build_tool_registry()` (`bot.py`) wrap the real LLM and registry in `InstrumentedLLM` / `InstrumentedToolRegistry`, which record `vektor_*` Prometheus metrics (tokens, latency, iterations, tool calls, notional cost) exposed on `/metrics` at `METRICS_PORT`. `HybridRetriever` additionally records `vektor_retrieval_expansion_total{status=ok|fallback}`, `vektor_retrieval_latency_seconds{stage=expansion|vector|fts|rerank|total}`, `vektor_retrieval_results{source=vector|fts|final}`, and `vektor_retrieval_rerank_total{status=ok|fallback}` — enum labels only. Hardening adds `vektor_exec_rejected_total{reason=metachar|not_allowed|bad_arg|parse}`, `vektor_rate_limited_total{reason=in_flight|window}` and `vektor_agent_budget_exceeded_total`. The exec tool logs only `argv0=<name>` (or `<other>` for a non-allowlisted name) and the outcome, never its arguments. `logging_config.configure_logging()` emits JSON logs with a `RedactionFilter`; when `LOKI_PUSH_URL` is set, a `LokiPushHandler` batches and pushes log records to Loki. Sensitive content (message text, prompts, responses, tool args/outputs) is never logged or metriced.

## Spec-driven workflow

Service behavior changes **only through an OpenSpec change**. `openspec/specs/` is the source of truth for behavior and is never edited by hand: first a change in `openspec/changes/<id>/` (proposal, tasks, spec deltas), then the implementation against its task list, then `openspec archive <id>` — the archive lands the deltas in `openspec/specs/` in the same PR that completes the tasks. CI fails a change whose tasks are all complete but which was not archived. Docs-only edits that do not change behavior (README, comments) do not need a change.

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
openspec validate --all --strict  # specs — same version as CI: npx --yes @fission-ai/openspec@1.14.1 validate --all --strict
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
- Retrieval tests (RRF, sqlite-vec store, chunking + per-page chunking, hybrid, expansion, rerank, kb tools, config, user isolation) use fakes, `httpx.MockTransport`, and tmp SQLite files — no Ollama/network needed.
- Exec tests: `tests/test_exec_policy.py` (hermetic — fake binaries in `tmp_path`, `Popen` tripwire/recorder) covers metachars, parse errors, the allowlist and env override, curl/DNS argument rules, forced curl flags, the rejection metric and argv0-only logging; `tests/test_exec.py` drives the venv `python3` (allowlisted via the `allow_python` fixture, `dirname(sys.executable)` on the search path, payloads as script files) for the output contract, truncation, timeouts, process-group kill, escaped-child return, the output byte cap, env scrub and temp cwd.
- Rate-limit tests: `tests/test_rate_limit.py` (fake clock, thread smoke) and `tests/test_bot_rate_limit.py` (handler wiring with `FakeLLM`/`ScriptedLLM`, fake TeleBot uploads, env parsing, worker threads) — offline.
- Message I/O tests (`tests/test_input_validation.py`, `tests/test_output_safety.py`) cover the pure `agent/validation.py` functions plus handler-level wiring with `ScriptedLLM`/`FakeLLM` and a fake TeleBot recorder — offline.
- Document tests (`tests/test_documents.py`) build fixtures in-test — a handcrafted minimal PDF (known `Tj` text operator), a python-docx-generated DOCX, plain bytes — fully offline, no binary fixtures committed.
- Document bot-flow tests (`tests/test_bot_documents.py`) use a fake TeleBot (records get_file/download_file, canned bytes or raises), a real kb stack over tmp SQLite with a deterministic fake embedder, and `ScriptedLLM`/`FakeLLM` — no network, no Ollama.
- Retrieval-benchmark tests cover only the scoring math and labeled dataset — offline.
- Docker config tests (`tests/test_docker_config.py`) check the hardening invariants of `Dockerfile` (runtime stage only), `compose.yaml` (the `vektor` service block), `.dockerignore` and the CI `docker` job with plain text/regex parsing — no pyyaml, no docker daemon, offline. The CI `docker` job builds the image and asserts the same invariants inside it.
- Live-model suites (`test_red_team.py`, `test_llm_judge.py`, `test_memory_behavior.py`, `test_latency_sla.py`) carry the `live` marker and are opt-in: `VEKTOR_LIVE_TESTS=1 python -m pytest -m live`. They skip when Ollama is down or a required model is not pulled (`tests/ollama_probe.py`). The red-team suite scrubs the environment to an allowlist (`tests/redteam_fragments.py`) before running model-chosen commands. Its dataset (`tests/datasets/redteam_dataset.json`) has 16 cases, 2 of them in Russian and 2 `exec-reject` cases (`exec-abuse-kill`, `exec-abuse-ps`) that assert via `exec_reject_violations` that no forbidden binary actually ran.
- Benchmarks run via `python -m benchmarks.run` (agent, requires a running Ollama) and `python -m benchmarks.retrieval_bench` (retrieval modes; Recall@K/Precision@K/latency, requires a running Ollama); both are excluded from pytest.

### Test coverage

- Agent: normal request, tool call → execution → result → next LLM call, multiple iterations, max iteration protection, wall-clock budget (between iterations and between tool calls of one reply, skipped results per call id, first iteration always runs, metric, env parsing), tool-call cap per reply, tool failure, unknown tool, LLM error, no Ollama dependency.
- CVE selector: latest-window selection, highest-score selection, equal-score tie-breaking, missing CVSS, first API result not automatically selected, CVE ID not used as recency proxy, ADP CVSS, CVSS v4, complex multi-window scenarios.
- Conversation: persistence within a chat, chat isolation, `/new`, `/new` only clears current chat.
- Tools: registration, execution, failure handling, unknown tool, adding tools without loop changes.
- Exec policy: `kill`/`ps`/`rm`/`sh`/path-prefixed binaries rejected with the allowlist shown, shell operators (pipe, `$`, redirects, backticks, `;`, `&&`, parens, newline, CR, NUL), parse errors, curl option allowlist (output/upload/data/config/proxy/resolve/unix-socket/`--url-query`/`-L` rejected, `@` in values and URLs, percent-encoded hosts, loopback/private/link-local/numeric/IPv4-mapped/compatible/NAT64/SIIT literals, globbing, multiple URLs), DNS/whois rules (no server, port, transport or second positional), forced curl flags, no spawn on reject, metric per reason, no secrets in logs, scrubbed env, temp cwd, rlimits requested, process-group kill, output byte cap, `killpg` fallback.
- Rate limit: in-flight and window rejections with one reply, no LLM call and untouched history, uploads/`/new`/validation rejects counted, independent users, release after errors, auth gate first, env fallbacks, worker-thread clamp.
- CveTool: highest-score selection, latest-window selection, tie-breaking, missing CVSS, partial fetch failures, deduplication, max-records limit, data-source attribution.
- Skill loader: discovers `.md` files, ignores non-`.md`, system prompt generation.
- Bot: agent routing, per-chat context in Telegram, `/new`, auth, LLM error handling, document flow (download/extract/ingest roundtrip, staged progress sequence ordering with the agent reply via the upload notice [+ caption], ingestion searchable via kb_search, upload context propagating to follow-up messages, PDF page numbers reaching the store while txt chunks keep a NULL page and the fact sheet omits it, user A's upload invisible to user B, documents row written with owner and file type, auth gate with no download, KB-disabled reply, error paths — unknown ext, corrupt bytes, API error, embedder failure, agent-turn LLM/agent errors, missing file_path — and document-wins-over-text).
- Bot commands: `/documents` listing (filename, created date, chunk count) scoped to the caller, empty state, auth gates, KB-disabled; `/delete` cascade visible in search, unknown/not-yours replies, usage hint (incl. filenames with spaces), command registration and precedence over the catch-all handler.
- Message I/O: empty/whitespace and 4096/4097-char rejection with one reply, no LLM call and untouched history, document-without-text still routed to the document flow, zero-width/bidi stripping with visible Unicode (emoji, Cyrillic) untouched, `split_reply` ordering/boundaries/content equality/unbreakable-token hard cut/astral emoji measured in UTF-16 units, long replies split on both text and document-upload turns, blank-chunk skip and single fallback, no `parse_mode` and metacharacters delivered unchanged.
- Documents: extraction per format (.txt/.md replacement decode, handcrafted PDF `Tj` text, python-docx paragraphs), `extract_pages` (single first page for txt/md/docx, one entry per PDF page incl. empty pages, join wrapper), case-insensitive extensions, unsupported/missing extension and empty filename → `DocumentError`, corrupt bytes → `DocumentError`, content-independence from filename, exact `SUPPORTED_EXTENSIONS`, no Telegram imports (AST-checked).
- Retrieval: RRF fusion and tie-breaking, vec0 store (one-transaction documents+chunks+FTS+vec0 writes, L2 normalization on write, zero-vector/multi-query min-distance merge, ties by chunk_id, lazy `vec_chunks` creation, page column persisted, legacy-schema DB → `KBModelError`), per-page chunking (page numbers pass through, chunks never span pages), HybridRetriever fallbacks/concurrency/metrics + rerank stage (reorders fused hits, fallback keeps RRF order), expansion ok/fallback parsing, kb ingest→search roundtrips, fact sheet with/without page, KB_ENABLED=0 regression, bot composition roots.
- User isolation: same-text ingest by two users never leaks chunks across owners, per-user scoping of search/list, delete by a non-owner returns False.
- Rerank: one LLM call scores all hits (JSON/float/bare-array/plain-number parsing, think-tag and code-fence tolerance), ties keep RRF order, scores clamped to 0–10, malformed/empty replies and LLM/unexpected errors fall back without raising, top-k truncation on success and fallback, RRF scores untouched.
- Retrieval benchmark: recall/precision@K scoring math (dedup, k-truncation, empty cases) and dataset coherence — offline.

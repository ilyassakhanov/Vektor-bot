# Vektor

A Telegram bot that bridges chats to a local LLM (via [Ollama](https://ollama.com)) with an autonomous agent and a retrieval-augmented (RAG) knowledge base over your own documents. Uses long-polling — no webhook or server required. Also includes a CVE summarizer that retrieves and summarizes the latest high-scoring CVE using the built-in agent.

## Features

- Telegram long-polling via [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI)
- Bounded agentic loop with tool calling (allowlisted `exec`, MCP CVE tool, knowledge-base tools), a wall-clock budget and per-user rate limiting
- RAG knowledge base: upload `.txt/.md/.pdf/.docx` → chunking → embeddings → hybrid retrieval (sqlite-vec ‖ FTS5 → RRF fusion → LLM rerank)
- Per-user isolation: every knowledge-base read and write is SQL-scoped by the Telegram user id
- `/documents` and `/delete` commands to manage your uploaded documents
- Answers cite their sources: `Источник: <filename>, стр. M` (PDF pages) or `chunk #N`
- Username-based access control; provider-agnostic LLM layer (Ollama included by default)

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit TELEGRAM_BOT_TOKEN
ollama pull llama3.2 qwen3:0.6b qwen3-embedding:0.6b
```

### Prerequisites

- Python 3.14+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- [Ollama](https://ollama.com) running locally (or adjust `OLLAMA_BASE_URL`) with the three models above
- sqlite-vec (installed via `requirements.txt`; loaded as a SQLite extension at runtime)

## Run

```bash
python bot.py
```

The bot polls Telegram for updates until you stop it with `Ctrl-C`.

## Run in Docker

The bot can also run in one hardened container. The bot, its MCP CVE subprocess and every `exec` child run inside it. Ollama and the monitoring stack stay on the host.

```bash
cp .env.example .env            # then edit TELEGRAM_BOT_TOKEN (same file as bare metal)
docker compose up -d --build
docker compose ps               # STATUS should become "healthy"
docker compose logs -f vektor
docker compose down             # stop; the KB volume is kept
```

- **Ollama** runs on the host. The container reaches it at `http://host.docker.internal:11434` (mapped to `host-gateway` in `compose.yaml`). Set `VEKTOR_OLLAMA_BASE_URL` in your shell to use another address.
- **Overrides** — `compose.yaml` sets `OLLAMA_BASE_URL`, `KB_DB_PATH=/data/vektor.db`, `METRICS_PORT=9101` and `LOKI_PUSH_URL` itself. These win over the same keys in `.env`. Everything else comes from `.env`.
- **Compose-only knobs** (shell variables, not `.env` keys): `VEKTOR_ENV_FILE` (env file path, default `.env`), `VEKTOR_IMAGE` (image tag, default `vektor:local`), `VEKTOR_OLLAMA_BASE_URL`, `VEKTOR_LOKI_PUSH_URL`.

### One-time KB migration

The knowledge base lives in the named volume `vektor-data`, mounted at `/data`. To bring over an existing `data/vektor.db`:

```bash
# 1. Stop the bare-metal bot first. Two pollers on one token make Telegram
#    answer 409 Conflict, and a stopped bot leaves a consistent SQLite file.
# 2. Create the container without starting it, then copy the file in.
docker compose create
docker compose cp data/vektor.db vektor:/data/vektor.db
# 3. docker compose cp creates the file as root; hand it to 10001:
docker compose run --rm --no-deps --user 0:0 --cap-add CHOWN --cap-add DAC_OVERRIDE \
  vektor chown 10001:10001 /data/vektor.db
docker compose up -d
```

### Monitoring

- `/metrics` is published only on the host loopback, `127.0.0.1:9101`. The k3s Prometheus target `192.168.5.2:9101` (the Mac loopback through the Lima gateway) doesn't change.
- Loki: set `VEKTOR_LOKI_PUSH_URL=http://loki.192.168.5.15.nip.io/loki/api/v1/push` in your shell. `compose.yaml` maps that host name to `host-gateway` (`extra_hosts`), so the push goes through the host's Traefik. Unset = logs go to stderr only.
- `docker compose logs vektor` always works as a fallback (json-file driver, 3 × 10 MB rotation).

### Engine choice

Rancher Desktop with the **dockerd (moby)** engine is the tested setup: `docker compose` works as is, and the monitoring addresses above assume Rancher's network. Docker Desktop also runs the container, but the Prometheus target and the Loki route are Rancher-specific, and two engines on one machine waste resources. Pick one.

### Hardening

| Control | What it stops |
|---|---|
| Non-root user `10001:10001`, nologin shell | Root inside the container; writing to the code in `/app` (owned by root) |
| `read_only: true` root filesystem, `/tmp` tmpfs capped at 64 MB | Changing binaries or code at run time; filling the disk |
| `cap_drop: ALL` | Raw sockets, chown and every other capability |
| `no-new-privileges:true` | Gaining privileges through setuid/setgid binaries |
| `pids_limit: 128`, `mem_limit: 1g`, `cpus: 1.5`, `nofile` 1024 | Fork bombs and resource exhaustion of the host |
| Process-control binaries removed from the image; the build fails if `ps`, `kill`, `pkill`, `pgrep`, `killall` or `top` is still there (`skill`/`snice` are removed too, unchecked) | Replaying the `ps \| grep python; kill -9` incident, even with a widened exec allowlist |
| `init: true` (tini as PID 1) | Zombie exec/MCP children; lost SIGTERM on stop |
| `restart: unless-stopped` + `HEALTHCHECK` on `/metrics` | A crash, OOM or self-kill keeping the bot down |
| Port published on `127.0.0.1` only | Exposing `/metrics` to the network |
| `.dockerignore` + explicit `COPY` list | Baking `.env`, `data/` or `tests/` into the image |
| `vektor-data` named volume on `/data` | Losing the KB on rebuild or restart |

### Residual risks

- Exec children share the UID and PID namespace with the bot. The guards are the exec allowlist and the missing process-control binaries. The blast radius is one container that restarts on its own.
- `curl` can resolve `host.docker.internal`, but the exec tool forces HTTPS and the host's services are plain HTTP.
- `sh`, `rm` and `python` stay in the image. They are reachable only if `EXEC_ALLOWED_COMMANDS` is widened.

### Manual verification

- [ ] `docker compose up -d --build`, then `docker compose ps` shows `healthy`.
- [ ] A Telegram message gets an answer, a CVE query works, a PDF upload ingests and can be searched.
- [ ] The KB survives `docker compose restart`: `/documents` still lists the upload.
- [ ] The Prometheus target `192.168.5.2:9101` is UP; Loki has `vektor` logs (when `VEKTOR_LOKI_PUSH_URL` is set).
- [ ] `docker compose exec vektor id -u` prints `10001`; `docker compose exec vektor touch /app/x` fails (read-only).
- [ ] Incident replay: ask the bot to run `ps -ax | grep python`, then `kill -9 <pid>`. Both are rejected by the exec tool (shell metacharacter, command not allowlisted).
- [ ] Even with a widened allowlist the binaries are gone: `docker compose exec vektor ps` fails with "executable file not found".
- [ ] Self-restart: kill the bot process from inside, then watch `docker compose ps` bring it back:
  `docker compose exec vektor python -c "import os; me=os.getpid(); [os.kill(int(p), 9) for p in os.listdir('/proc') if p.isdigit() and int(p) != me and open(f'/proc/{p}/cmdline','rb').read() == b'python\x00bot.py\x00']"`

## Usage

```
/new               - start a new chat (clears this chat's LLM context)
/documents         - list your uploaded documents
/delete <filename> - remove one of your documents from the knowledge base

Upload a document (.txt, .md, .pdf, .docx) as a file attachment — it is
chunked, embedded and stored, then you can ask questions about it.

Mention <<using CVE skill>>
example: Bring me up to speed with latest CVEs with cve skill
```

## Architecture

```
Telegram message
      │
      ▼
handle_message / handle_document ── auth gate (ALLOWED_USERNAMES) FIRST,
      │                            user context captured here
      ▼
ConversationManager → Agent (bounded loop, skills in system prompt)
      │
      ├── kb_search(query) ──────────────────────────────────────────┐
      │        │                                                    │
      │        ▼                                                    │
      │  Query expansion (qwen3:0.6b, temp 0)                       │
      │        │                                                    │
      │        ▼                                                    │
      │  Embed [original + alt queries] (qwen3-embedding:0.6b)      │
      │        │                                                    │
      │        ├─────────────────┬─────────────────┐                │
      │        ▼                 ▼                 │                │
      │  sqlite-vec KNN       FTS5 BM25      (parallel threads,      │
      │  (owner-filtered)     (owner-filtered)  owner passed down)   │
      │        └────────┬────────┘                                    │
      │                 ▼                                             │
      │        RRF fusion (k=60) → top-k → LLM rerank (pointwise)     │
      │                 │                                             │
      │                 ▼                                             │
      │        compact fact sheet ────────────────────────────────────┘
      │
      └── exec, get_latest_cve (MCP stdio subprocess)

Agent → LLM (llama3.2) → final answer + «Источник:» citations
```

Document ingestion happens bot-level, before the agent runs:

```
Telegram document → get_file + download_file → documents.extract_pages
                  → chunking (per page for PDF) → embeddings → SQLite
                  → staged progress replies → synthetic upload notice
                  → ConversationManager → Agent (acknowledges / answers caption)
```

The Telegram handler depends only on the `LLM` interface (`llm/base.py`), never on a concrete provider. To add a provider: create `llm/<provider>.py` implementing `LLM`, then swap it in via `build_llm()` in `bot.py`.

## Design decisions

### Chunking (800 chars / 100 overlap)

Documents are split into word-aligned windows of `KB_CHUNK_SIZE=800` characters with a `KB_CHUNK_OVERLAP=100`-character overlap between consecutive chunks.

- **Why 800:** roughly a few paragraphs — large enough that a chunk keeps the claim it makes together with its context, small enough that a chunk stays on one topic.
- **Why overlap:** a fact stated across a window boundary would otherwise be cut in half; the overlap guarantees every sentence appears intact in at least one chunk.
- **Too-small chunks** lose context: retrieval returns fragments, embeddings of tiny snippets are less informative, and the LLM must read many more chunks for the same content.
- **Too-big chunks** dilute similarity: one chunk mixing topics produces a blurred embedding, the top-k budget gets spent on mostly-irrelevant text, and large chunks waste prompt space.
- PDFs are chunked **per page** (`chunk_pages`): chunks never span a page boundary, and every PDF chunk carries its page number (non-PDF chunks have no page).

### Embeddings

`qwen3-embedding:0.6b` (default `OLLAMA_EMBED_MODEL`), 1024 dimensions, served by the same local Ollama via `/api/embed`.

- **Local** — no API keys, no cost, document text never leaves the machine.
- **Fast** — a 0.6B model keeps per-query embedding latency low.
- **Strong multilingual retrieval** — documents and questions work in Russian and English alike.

### Retrieval

- **Hybrid**: semantic vector search (sqlite-vec KNN) and keyword search (SQLite FTS5 / BM25) run in parallel threads; expansion keywords go to the FTS side only (they are BM25 terms, not sentences).
- **Cosine-equivalent ranking**: vectors are L2-normalized on write, so the vec0 default distance orders chunks exactly like cosine similarity (the cosine distance metric option is not supported by sqlite-vec 0.1.9).
- **RRF (k=60)** fuses the two ranked lists by rank only — raw per-source scores are never mixed.
- **Rerank stage**: one pointwise LLM call (the small expansion model) scores all fused hits 0–10 for query relevance, then re-sorts (ties keep RRF order) and truncates to top-k. It never raises: on any failure or timeout (`KB_RERANK_TIMEOUT`) it falls back to the RRF order.
- **Top-k = 5** (`KB_TOP_K`) hits are rendered as a compact fact sheet (title, chunk number, page when present, capped content — raw scores never shown) for the LLM.
- **Fallbacks, never failures**: expansion degrades to the original query; a failing source is skipped while the other still answers; FTS5 unavailable or `KB_FTS_ENABLED=0` → vector-only mode.

### Storage

One SQLite file (`KB_DB_PATH`, default `data/vektor.db`), four tables plus metadata:

| Table | Contents |
|---|---|
| `documents` | `id` (sha256 of text, first 16 hex chars), `user_id`, `filename`, `file_type`, `created_at` (ISO-UTC) |
| `chunks` | `id` (stable `doc_id:idx` hash), `document_id`, `chunk_index`, `text`, `page` (PDF only, NULL otherwise), `embedding` (normalized float32 BLOB) |
| `vec_chunks` | sqlite-vec `vec0` virtual table (`chunk_id`, `owner` aux column, `embedding FLOAT[dim]`) — created **lazily at first ingest**, once the embedding dimension is known |
| `chunks_fts` | FTS5 index, synced with `chunks` in the same transaction |

- Every ingest writes `documents` + `chunks` + `chunks_fts` + `vec_chunks` in a **single transaction**; re-ingesting the same text is an upsert (stable sha256 ids).
- Deletion (`/delete`, `delete_document`) cascades all four tables in one transaction.
- A legacy-schema database (pre-`documents`) is refused at startup with `KBModelError` telling the user to delete the DB file and re-ingest.
- Uploading the **same text** by two users collides on the document id — the document row is owned by the last writer (each user's chunks stay owner-scoped).

### User isolation

- The principal is the Telegram numeric `from_user.id` (stable across `@username` changes), captured in a `ContextVar` (`retrieval/principal.py`) right after the auth gate and reset in `finally`.
- The id is passed **down as an explicit argument** (`HybridRetriever.search(query, user_id)`) because contextvars do not cross the retrieval `ThreadPoolExecutor` boundary.
- Isolation is **SQL-level, not convention-level**: every store read (`search_vec`, `search_fts`, `list_documents`, `delete_document`) takes a mandatory `user_id`; the vec0 KNN query pre-filters on the `owner` aux column. There is no unfiltered search entry point.
- **The owner is never LLM-controlled**: tool schemas are `kb_ingest {text, title}` and `kb_search {query}` — no `user_id` property anywhere; the principal comes from Telegram, not from prompt text.
- `skills/documents.md` hardens against hallucination: every fact taken from `kb_search` must be cited (`Источник: <filename>, стр. M`, or `, chunk #N` when there is no page), and if nothing relevant is found the model must say so instead of answering from general knowledge.

### Security

- The `ALLOWED_USERNAMES` allowlist is the auth gate and runs FIRST — unauthorized users' messages and documents are never processed or downloaded. It is an access gate only; multiple users are fine because KB data is scoped per numeric id.
- Everything runs locally: Ollama + one SQLite file. No third-party API calls.
- Secrets live in `.env` (gitignored); the MCP CVE server subprocess runs with a scrubbed environment.
- The `exec` tool never uses a shell: it runs only allowlisted, network read-only commands (`curl` over https to public hosts, `dig`, `nslookup`, `whois`, `host`) with per-command argument rules, a scrubbed environment, a temporary working directory, resource limits, a byte-capped output and a process-group kill on timeout. Process, file and host commands (`ps`, `kill`, `rm`, …) are rejected in code, whatever the model is told.
- Each user gets one in-flight message and a sliding-window message limit; each agent run has a wall-clock budget and a per-reply tool-call cap.
- Logs are JSON with redaction; message text, prompts, tool arguments/outputs and file contents are never logged.

### Bonus features

- **Staged progress messages** — an upload replies `📄 Document received` → `⏳ Extracting text…` → `⏳ Generating embeddings…` → `✅ Ingested N chunks (doc <id>, title '<file>')` → `✅ Document ready. Now you can ask questions.` before the agent's reply. Failures always end with exactly one friendly error reply; polling never crashes.
- **PDF page numbers + citations** — per-page extraction (`extract_pages`) keeps page numbers on chunks; the skill mandates `Источник: <filename>, стр. M` citations (or `chunk #N` for non-PDF).
- **Hybrid search** — vector ‖ FTS5 fused with RRF (see Retrieval above).
- **Reranking** — pointwise LLM rerank behind `KB_RERANK_ENABLED` (see Retrieval above).
- **Conversation-aware RAG** — the upload turn is recorded in the chat's conversation as a synthetic notice (`[document uploaded: "<name>", N chunks ingested; content is now searchable via kb_search]`, caption appended when present), so follow-up questions in the same chat are answered from the document without re-uploading.

## Limitations

- **Single-machine KB** — one local SQLite file; no sync, replication, or multi-host access.
- **Chunks never span pages** — a paragraph crossing a PDF page boundary is split at the boundary (page-precise citations win over cross-page chunks).
- **20 MB upload cap** — Telegram limits bot file downloads to 20 MB.
- **Allowlist-only auth** — Telegram username tags, no passwords or 2FA; the KB has no per-user encryption.
- **Same-text ownership** — identical text uploaded by two users resolves to one document id; the last writer owns it.

## Configuration

Secrets and settings live in `.env` (gitignored). Real environment variables always take precedence over `.env` values. See `.env.example` for the full annotated list.

| Key | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token (must contain a colon) |
| `OLLAMA_BASE_URL` | no | Ollama HTTP base URL (default `http://localhost:11434`) |
| `OLLAMA_MODEL` | no | Agent model (default `llama3.2`) |
| `OLLAMA_EXPANSION_MODEL` | no | Query-expansion + rerank model (default `qwen3:0.6b`) |
| `OLLAMA_EMBED_MODEL` | no | Embedding model (default `qwen3-embedding:0.6b`) |
| `ALLOWED_USERNAMES` | no | Comma-separated Telegram usernames allowed to use the bot (empty = none allowed) |
| `KB_ENABLED` | no | Register `kb_ingest`/`kb_search` and document uploads (default `1`) |
| `KB_DB_PATH` | no | SQLite knowledge-base file (default `data/vektor.db`) |
| `KB_CHUNK_SIZE` / `KB_CHUNK_OVERLAP` | no | Chunking window and word-aligned overlap (default `800` / `100`) |
| `KB_TOP_K` | no | Fused hits returned to the LLM (default `5`) |
| `KB_RRF_K` | no | RRF k constant (default `60`) |
| `KB_VECTOR_LIMIT` / `KB_FTS_LIMIT` | no | Per-source result limits (default `20` / `20`) |
| `KB_FTS_ENABLED` | no | FTS5 keyword source (default `1`; `0` or missing FTS5 → vector-only) |
| `KB_EXPANSION_ENABLED` | no | LLM query expansion before retrieval (default `1`) |
| `KB_RERANK_ENABLED` | no | Pointwise LLM rerank after RRF (default `1`) |
| `KB_RERANK_TIMEOUT` | no | Rerank LLM call timeout in seconds (default `10.0`) |
| `METRICS_PORT` | no | Prometheus `/metrics` port (default `9100`) |

## Project structure

| File | Purpose |
|---|---|
| `bot.py` | Entrypoint — composition root: wires TeleBot, Agent, ConversationManager, LLM, KB stack, command handlers |
| `config.py` | `.env` loader (stdlib only) |
| `documents.py` | Text extraction — `extract_pages()` / `extract_text()` for .txt/.md/.pdf/.docx (pypdf + python-docx, Telegram-free) |
| `llm/` | Provider-agnostic `LLM` interface (`base.py`), `OllamaLLM` (`ollama.py`), metrics wrapper (`instrumented.py`) |
| `agent/` | Bounded agentic loop, per-chat `ConversationManager`, CVE selector |
| `tools/` | `Tool` interface, registry, `ExecTool` (allowlisted, shell-free, sandboxed), MCP client + CVE tool adapter, `kb_ingest`/`kb_search` |
| `skills/` | `.md` instruction files discovered dynamically and injected into the system prompt |
| `retrieval/` | Chunking, embeddings, query expansion, sqlite-vec + FTS5 store, RRF fusion, reranker, hybrid retriever, per-user principal |
| `mcp_servers/cve_server.py` | CVE MCP tool served over stdio JSON-RPC |
| `metrics.py`, `logging_config.py`, `loki_handler.py` | Prometheus metrics, JSON logging with redaction, optional Loki push |
| `Dockerfile`, `compose.yaml`, `.dockerignore` | Hardened single-container runtime (see Run in Docker) |

## Testing

```bash
python -m pytest
```

- No network, Ollama, or Telegram needed: tests use fakes (`FakeLLM`, `ScriptedLLM`, fake TeleBot, deterministic fake embedder), `httpx.MockTransport`, and temporary SQLite files.
- `tests/conftest.py` sets a dummy `TELEGRAM_BOT_TOKEN` so `bot.py` imports without real secrets.
- User isolation, rerank, command routing, document roundtrips and citation rules are all covered offline; see `AGENTS.md` for the detailed coverage list.
- Benchmarks (`python -m benchmarks.run`, `python -m benchmarks.retrieval_bench`) require a running Ollama and are excluded from pytest.

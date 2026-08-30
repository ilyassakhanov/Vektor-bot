# Vektor — Telegram long-polling bot with autonomous AI agent

**Stack:** Python 3.14, [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI), [httpx](https://www.python-httpx.org/), [ruff](https://docs.astral.sh/ruff/), [mypy](https://mypy-lang.org/)

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit TELEGRAM_BOT_TOKEN
```

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
| `ALLOWED_USERNAMES` | no | Comma-separated Telegram usernames (tags) allowed to use the bot, e.g. `@some-user,@another-user` (empty = none allowed) |
| `EXEC_TIMEOUT` | no | Timeout in seconds for the `exec` tool (default `30`) |
| `AGENT_MAX_ITERATIONS` | no | Maximum agent loop iterations (default `8`) |

## Project structure

| File | Purpose |
|---|---|
| `bot.py` | Entrypoint — composition root, wires TeleBot with Agent + ConversationManager + LLM |
| `config.py` | `.env` loader (stdlib only) |
| `.env.example` | Template for `.env` |
| `llm/base.py` | Abstract `LLM` interface, `LLMResponse`, `ChatResponse`, `Message`, `ToolSpec`, `ToolCall`, `ToolResult`, `LLMError` |
| `llm/ollama.py` | `OllamaLLM` — Ollama HTTP API via `httpx` (no Ollama SDK) |
| `llm/__init__.py` | Re-exports LLM types; lazy-loads `OllamaLLM` |
| `agent/agent.py` | `Agent` — bounded agentic loop (default 8 iterations) |
| `agent/conversation.py` | `ConversationManager` — per-chat context, `/new` command |
| `agent/cve_selector.py` | `select_cve()` — deterministic latest-window + highest-score CVE selection |
| `tools/base.py` | Abstract `Tool` interface, `ToolError` |
| `tools/registry.py` | `ToolRegistry` — agent invokes tools via registry |
| `tools/exec.py` | `ExecTool` — generic shell execution with timeout, returns stdout/stderr/exit code |
| `tools/cve.py` | `CveTool` — retrieves recent CVE records and selects the most critical one programmatically |
| `skills/loader.py` | `SkillLoader` — discovers `.md` skill files dynamically |
| `skills/cve.md` | CVE workflow skill — instructions for using the `get_latest_cve` tool |
| `mypy.ini` | mypy configuration |

## Architecture

```text
Telegram → ConversationManager → Agent → LLM interface → OllamaLLM
                                    │
                                    ├── ToolRegistry → ExecTool (shell, curl)
                                    │                → CveTool (programmatic CVE retrieval + selection)
                                    └── SkillLoader → skills/*.md
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

- `ExecTool` — executes a shell command, returns stdout/stderr/exit code, enforces a configurable timeout. Generic — no CVE-specific logic.
- `CveTool` — retrieves recent CVE records from official CVE.org endpoints and selects the most critical one programmatically (via `select_cve()`). Returns a compact fact sheet so the LLM only needs to summarize — no JSON parsing, score comparison, or windowing on the LLM side.

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

## Code conventions

- `from __future__ import annotations` in every module
- Logging via module-level logger (`log = logging.getLogger("vektor.xxx")`)
- `handle_message(message, conv, reply_to)` (`bot.py`) is the testable handler core — it takes an injected `ConversationManager` and a `reply_to` callable
- `create_bot(conv)` (`bot.py`) wraps `handle_message` in a TeleBot handler — add new handlers there
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

### Test coverage

- Agent: normal request, tool call → execution → result → next LLM call, multiple iterations, max iteration protection, tool failure, unknown tool, LLM error, no Ollama dependency.
- CVE selector: latest-window selection, highest-score selection, equal-score tie-breaking, missing CVSS, first API result not automatically selected, CVE ID not used as recency proxy, ADP CVSS, CVSS v4, complex multi-window scenarios.
- Conversation: persistence within a chat, chat isolation, `/new`, `/new` only clears current chat.
- Tools: registration, execution, failure handling, unknown tool, adding tools without loop changes.
- CveTool: highest-score selection, latest-window selection, tie-breaking, missing CVSS, partial fetch failures, deduplication, max-records limit, data-source attribution.
- Skill loader: discovers `.md` files, ignores non-`.md`, system prompt generation.
- Bot: agent routing, per-chat context in Telegram, `/new`, auth, LLM error handling.

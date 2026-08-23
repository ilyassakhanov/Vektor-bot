# Vektor — Telegram long-polling bot

**Stack:** Python 3.14, [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI), [httpx](https://www.python-httpx.org/)

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

## Project structure

| File | Purpose |
|---|---|
| `bot.py` | Entrypoint — composition root, wires TeleBot with an `LLM` provider |
| `config.py` | `.env` loader (stdlib only) |
| `.env.example` | Template for `.env` |
| `llm/base.py` | Abstract `LLM` interface, `LLMResponse`, `LLMError` — provider-agnostic |
| `llm/ollama.py` | `OllamaLLM` — Ollama HTTP API via `httpx` (no Ollama SDK) |
| `llm/__init__.py` | Re-exports `LLM`, `LLMError`, `LLMResponse`; lazy-loads `OllamaLLM` |

## Architecture: LLM layer

The Telegram handler depends **only** on the `LLM` interface (`llm/base.py`), never on a concrete provider.

```text
bot.py (composition root) — selects provider from env
       │
   create_bot(llm) — injects provider into the handler
       │
   handle_message() — calls llm.generate(), replies to user
       │
   LLM interface (llm/base.py)
       │
   OllamaLLM (llm/ollama.py) — httpx → Ollama HTTP API
```

- Provider is selected in `build_llm()` (`bot.py`) — the handler is provider-agnostic.
- To add a provider: create `llm/<provider>.py` implementing `LLM`, then swap it in `build_llm()`.
- All Ollama request/response handling is confined to `llm/ollama.py`.
- LLM errors surface as `LLMError`; the handler catches them and sends a user-friendly message.

## Code conventions

- `from __future__ import annotations` in every module
- Logging via module-level logger (`log = logging.getLogger("vektor.xxx")`)
- `handle_message(message, llm, reply_to)` (`bot.py:35`) is the testable handler core — it takes an injected `llm` and a `reply_to` callable
- `create_bot(llm)` (`bot.py:58`) wraps `handle_message` in a TeleBot handler — add new handlers there

## Testing

```bash
python -m pytest
```

- `tests/conftest.py` sets a dummy `TELEGRAM_BOT_TOKEN` so `bot.py` imports without real secrets.
- `tests/fakes.py` provides `FakeLLM` — a mock `LLM` for Telegram handler tests (no Ollama needed).
- Ollama tests use `httpx.MockTransport` to simulate success/failure without a running Ollama instance.

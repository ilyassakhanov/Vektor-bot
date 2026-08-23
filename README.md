# Vektor

A simple Telegram bot that bridges chats to a local LLM (via [Ollama](https://ollama.com)). Uses long-polling — no webhook or server required.

## Features

- Telegram long-polling via [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI)
- Pluggable LLM provider layer (Ollama included by default)
- Username-based access control — only allowed Telegram tags can use the bot
- Provider-agnostic handler: swap LLMs without touching the bot logic

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit TELEGRAM_BOT_TOKEN
```

### Prerequisites

- Python 3.14+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- [Ollama](https://ollama.com) running locally (or adjust `OLLAMA_BASE_URL`)

## Run

```bash
python bot.py
```

The bot polls Telegram for updates until you stop it with `Ctrl-C`.

## Configuration

Secrets and settings live in `.env` (gitignored). Real environment variables always take precedence over `.env` values.

| Key | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token (must contain a colon) |
| `OLLAMA_BASE_URL` | no | Ollama HTTP base URL (default `http://localhost:11434`) |
| `OLLAMA_MODEL` | no | Ollama model name (default `llama3.2`) |
| `ALLOWED_USERNAMES` | no | Comma-separated Telegram usernames (tags) allowed to use the bot, e.g. `@some-user,@another-user` (empty = none allowed) |

## Architecture

```
bot.py (composition root) — selects provider from env, wires up TeleBot
       │
   create_bot(llm, allowed_usernames)
       │
   handle_message() — checks auth, calls llm.generate(), replies to user
       │
   LLM interface (llm/base.py)
       │
   OllamaLLM (llm/ollama.py) — httpx → Ollama HTTP API
```

The Telegram handler depends only on the `LLM` interface (`llm/base.py`), never on a concrete provider. To add a provider:

1. Create `llm/<provider>.py` implementing `LLM`.
2. Swap it in via `build_llm()` in `bot.py`.

### Project structure

| File | Purpose |
|---|---|
| `bot.py` | Entrypoint — composition root, wires TeleBot with an `LLM` provider |
| `config.py` | `.env` loader (stdlib only) |
| `.env.example` | Template for `.env` |
| `llm/base.py` | Abstract `LLM` interface, `LLMResponse`, `LLMError` — provider-agnostic |
| `llm/ollama.py` | `OllamaLLM` — Ollama HTTP API via `httpx` (no Ollama SDK) |
| `llm/__init__.py` | Re-exports `LLM`, `LLMError`, `LLMResponse`; lazy-loads `OllamaLLM` |

## Testing

```bash
python -m pytest
```

- `tests/conftest.py` sets a dummy `TELEGRAM_BOT_TOKEN` so `bot.py` imports without real secrets.
- `tests/fakes.py` provides `FakeLLM` — a mock `LLM` for Telegram handler tests (no Ollama needed).
- Ollama tests use `httpx.MockTransport` to simulate success/failure without a running Ollama instance.

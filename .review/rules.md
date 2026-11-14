# Review rules for Vektor

Team rules for the dmc-268 review bot. Only `##` sections with a `pattern:` line and numbered
checks are rules; paragraphs like this one are notes for people and are not sent to the model.
Security rules come first: rules enter the review context in file order, and on a large PR the
last ones may not fit. Format reference: dmc-268 `docs/RULES_GUIDE.md`.

## Security in Code

- pattern: `*.py`
- exclude: `tests/`
- exclude: `benchmarks/`

1. No Telegram token, API key or password is hard-coded: secrets are read from the environment (`os.environ` after `config.load_env()`)
2. A secret or token is never written to a log record, an exception message or a reply sent to the user
3. SQL receives values as `?` parameters, never via string concatenation, `%` or f-strings
4. Outside `ExecTool` (`tools/exec.py`, which runs agent shell commands by design), a subprocess is started with an argument list and without `shell=True`, and text from a Telegram message is never interpolated into a command
5. Untrusted data is not deserialized unsafely: no `pickle`, no `yaml.load` without `SafeLoader`, no `eval`/`exec` of model or user text
6. TLS verification is not disabled (`verify=False`)

## Per-User Isolation

- pattern: `retrieval/`
- pattern: `documents.py`
- pattern: `bot.py`

1. Every knowledge-base read and write is scoped by the Telegram numeric user id, not by username or chat title
2. A new bot command checks the `ALLOWED_USERNAMES` gate before doing any work

## Outbound HTTP

- pattern: `*.py`
- exclude: `tests/`

1. Every `httpx` client or call has an explicit timeout
2. Code that talks to the network accepts an injectable `httpx.Client` so tests can pass `httpx.MockTransport`

## Configuration

- pattern: `*.py`
- pattern: `.env.example`
- exclude: `tests/`

1. A new environment variable has a default in code, an entry in `.env.example` and a row in the Environment table of `AGENTS.md`
2. A numeric setting from the environment is parsed with a fallback to its default on an invalid value, not left to crash at startup

## Tests

- pattern: `tests/`

1. A test does not call Ollama or the network: it uses `FakeLLM`/`ScriptedLLM` from `tests/fakes.py` or `httpx.MockTransport`
2. A test that needs storage uses a temporary SQLite file (`tmp_path`), not `data/vektor.db`
3. A test that reads `TELEGRAM_BOT_TOKEN` relies on the dummy value from `tests/conftest.py`, not a real token

# Tasks

## 1. Per-chat serialization

- [x] 1.1 Threaded tests: two concurrent runs on one chat do not interleave, `/new` waits for a running run, other chats are not blocked, the lock is released when a run raises — `python -m pytest tests/test_conversation.py`
- [x] 1.2 `ConversationManager`: per-chat lock held for run + trim and for `/new`; no global lock across runs

## 2. CVE discovery deadline

- [x] 2.1 Tests: discovery request timeout capped to the time left, slow discovery keeps the call inside the timeout, no discovery past the deadline — `python -m pytest tests/test_cve_core.py`
- [x] 2.2 `cve_core._discover_cve_ids` takes the run deadline and caps its request timeout

## 3. Tie-break on parsed instants

- [x] 3.1 Tests: mixed-offset tie (`12:02+02:00` vs `10:04Z`), naive = UTC, unparseable ranks oldest — `python -m pytest tests/test_cve_selector.py`
- [x] 3.2 `select_cve_window` tie key uses the parsed timestamp

## 4. AGENT_MAX_ITERATIONS fallback

- [x] 4.1 Tests: unset, valid, `0`, negative, non-integer — `python -m pytest tests/test_agent.py`
- [x] 4.2 `build_agent` parses via `_positive_env` (warn and fall back to 8)

## 5. Docs, quality gates and archive

- [x] 5.1 AGENTS.md env table, Conversation and CVE selection notes
- [x] 5.2 `ruff check .`, `ruff format --check .`, `mypy .` pass
- [x] 5.3 `python -m pytest` passes offline
- [x] 5.4 `npx --yes @fission-ai/openspec@1.14.1 validate --all --strict` passes
- [x] 5.5 Archive this change in the same pull request that closes its last task

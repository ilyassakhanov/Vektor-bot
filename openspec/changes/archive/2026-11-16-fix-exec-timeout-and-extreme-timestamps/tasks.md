# Tasks

## 1. EXEC_TIMEOUT fallback

- [x] 1.1 Tests: unset, valid, `abc`, `nan`, `inf`, `0`, `-5` at `cve_core`, `McpStdioClient` and `build_tool_registry` — `python -m pytest tests/test_exec_timeout_env.py`
- [x] 1.2 Replace the three ad-hoc parsers with `config.positive_env("EXEC_TIMEOUT", 30.0, float)`

## 2. Extreme timestamps

- [x] 2.1 Tests: year-0001 record beside normal records, lone year-0001 record, offsets near `datetime.min`/`datetime.max` — `python -m pytest tests/test_cve_selector.py`
- [x] 2.2 `_group_windows` compares `anchor - at` instead of computing `at - span`

## 3. Docs

- [x] 3.1 AGENTS.md: `EXEC_TIMEOUT` row and test-coverage lines

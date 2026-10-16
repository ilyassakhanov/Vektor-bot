# Tasks

## 1. Scaffolding

- [x] 1.1 Add `openspec/config.yaml` — spec-driven schema, project context, artifact rules, apply/archive guidance
- [x] 1.2 Verify the pinned CLI runs from a clean checkout: `npx --yes @fission-ai/openspec@1.14.1 --version`

## 2. Baseline specs

- [x] 2.1 Add `agent-loop` delta: bounded iterations, registry-only tool calls, provider-agnostic LLM boundary, skill injection
- [x] 2.2 Add `conversation` delta: per-chat isolation, context cap with latest-message guarantee, `/new`
- [x] 2.3 Add `kb-retrieval` delta: pipeline order, degradation rules, storage invariants, fact-sheet rendering
- [x] 2.4 Add `kb-user-isolation` delta: mandatory owner filter, principal capture, non-LLM-controlled owner
- [x] 2.5 Add `documents` delta: auth gate first, bot-level ingestion before the agent, staged replies, never-crash, source attribution
- [x] 2.6 Add `cve-tool` delta: deterministic selection algorithm, MCP delivery, capped fact sheet
- [x] 2.7 Add `observability` delta: metric families, JSON logs, redaction guarantees
- [x] 2.8 Cross-check every requirement against the test suite (`python -m pytest`) — no requirement without a verifying test or an explicit statement of documented limitation

## 3. Workflow wiring

- [x] 3.1 State the rule in `AGENTS.md`: behavior changes only through an OpenSpec change; `openspec/specs/` is never hand-edited
- [x] 3.2 Add `.github/workflows/ci.yml`: `openspec validate --all --strict` and the quality gates (ruff, ruff format, mypy, pytest)
- [x] 3.3 Add `scripts/check_openspec_archived.py`: fail when a change has all tasks complete but was not archived
- [x] 3.4 Add `requirements-dev.txt` so CI and local runs install the same dev tools

## 4. Validation

- [x] 4.1 `npx --yes @fission-ai/openspec@1.14.1 validate --all --strict` passes
- [x] 4.2 `npx --yes @fission-ai/openspec@1.14.1 list --json | python3 scripts/check_openspec_archived.py` passes
- [x] 4.3 Local quality gates pass: `ruff check .`, `ruff format --check .`, `mypy .`, `python -m pytest`
- [x] 4.4 Archive this change so the deltas land in `openspec/specs/`

# Proposal

## Why

Vektor adopts a spec-driven workflow: from now on service behavior changes
only through an OpenSpec change, and `openspec/specs/` becomes the source of
truth for behavior. That rule is only honest if the specs describe what
exists today — otherwise the first real change would have to argue against
an empty baseline.

This change backfills the baseline. It documents current, tested behavior as
seven capability specs. No code changes: every requirement below is verified
by the existing test suite and read off the implementation.

## What Changes

- Adds `openspec/config.yaml` (spec-driven schema, project context, artifact
  rules, apply/archive guidance).
- Adds seven capability specs under `openspec/specs/` covering the whole
  service surface: agent-loop, conversation, kb-retrieval, kb-user-isolation,
  documents, cve-tool, observability.
- Wires validation into CI (`openspec validate --all --strict` + an
  archive-discipline check) and states the workflow rule in `AGENTS.md`.

## Capabilities

### Added Capabilities

- `agent-loop` — the bounded agent loop: LLM calls, tool execution via the
  registry, iteration cap, provider-agnostic LLM boundary, skill injection.
- `conversation` — per-chat conversation memory, context cap, `/new`.
- `kb-retrieval` — the hybrid retrieval pipeline, its degradation rules and
  storage invariants, and the fact sheet the agent sees.
- `kb-user-isolation` — the mandatory owner filter on every knowledge-base
  read and the principal that feeds it.
- `documents` — the bot-level document ingestion flow: auth gate, staged
  progress replies, never-crash guarantees, source attribution.
- `cve-tool` — deterministic latest-window CVE selection and its MCP delivery.
- `observability` — Prometheus metrics, JSON logs, redaction guarantees.

## Impact

- Documentation-only for the runtime: no Python files change, the bot's
  behavior is identical before and after.
- CI gains its first workflow file; the repo had none.
- The ad-hoc `tasks/plan.md` + `tasks/todo.md` planning pattern is superseded
  by OpenSpec changes for anything that changes behavior; existing files stay
  as history.
- Risks: a spec written from code can drift the day after it lands. That is
  what the workflow rule is for — drift is fixed by a change, not by editing
  `openspec/specs/` by hand.

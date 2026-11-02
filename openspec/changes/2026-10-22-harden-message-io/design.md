# Design

## Context

`bot.handle_message` is the single boundary every text message crosses:
auth gate → document routing → `ConversationManager.handle` →
`Agent.run` → one `reply_to` call. Neither the inbound text nor the outbound
reply is size- or content-checked anywhere today. Telegram's protocol caps
one message at 4096 characters on both sides (inbound text, outbound send),
which makes that boundary the right — and only — place to enforce safety.

## Goals / Non-Goals

Goals:

- Reject degenerate input (empty/blank, oversized) with one friendly reply
  and zero side effects (no LLM call, no document download, no history
  mutation).
- Strip invisible control characters that poison LLM context (zero-width,
  bidi overrides) without touching visible content.
- Deliver long agent replies as ordered message sequences with no content
  lost.
- Lock the plain-text reply contract in tests.

Non-Goals:

- No truncation of oversized input — rejected, not shortened (decision
  below).
- No MarkdownV2 rendering or escaping — replies stay plain text.
- No validation inside `Agent` or `ConversationManager` — the handler
  boundary owns it.
- No env knob for the 4096 limit — it is a Telegram protocol constant, not
  a tuning value; the warn-and-fallback env convention applies to tuning
  knobs, not protocol limits.

## Realization

- `agent/validation.py` — two pure functions, no I/O:
  - `validate_input(text) -> Verdict` — `ok` (scrubbed text) or `reject`
    (reason); rejects empty/blank and `len > 4096`; strips zero-width
    (`U+200B`-family) and bidi-override (`U+202A`-family, `U+2066`-family)
    control characters from accepted text.
  - `split_reply(text, limit=4096) -> list[str]` — ordered chunks, each at
    most `limit` characters; split preference paragraph → sentence → word;
    hard-cut fallback for an unbreakable token longer than `limit`;
    concatenation of chunks always equals the input.
- `bot.py` wiring:
  - `handle_message`: after the auth gate, before document/agent routing,
    validate; a rejection sends one friendly constant reply and returns.
  - Both agent-reply send sites (text path, document-handler agent turn)
    send `split_reply(response)` chunks in order via `reply_to`.
  - Friendly rejection replies are module constants in the existing
    `_…_REPLY` style.
- Tests: pure-function tests plus handler-level tests with the existing
  `FakeLLM`/`ScriptedLLM` and fake-TeleBot recorders — rejection paths
  assert `llm.chat_calls == []` and untouched conversation history; send
  paths assert per-chunk lengths, order, and that no `parse_mode` kwarg is
  ever recorded.

## Decisions

- Reject oversized input rather than truncate (agreed with maintainer): a
  silently shortened question can lose the actual question (usually at the
  end); an honest ask-to-shorten reply is better UX and cheaper than a
  wrong answer to a beheaded prompt.
- Split oversized output rather than truncate (agreed with maintainer): no
  content is lost; ordered sequential sends are acceptable.
- Validation placement — handler boundary, after the auth gate: unauthorized
  senders are answered by the gate and never reach validation; rejected
  messages never enter `ConversationManager`, so history stays clean
  without any change to the conversation manager.
- Commands are unaffected: `/documents` and `/delete` are separate TeleBot
  handlers registered before the catch-all; `/new` is non-empty text that
  reaches `ConversationManager` and is matched exactly — validation never
  sees a message it could misclassify as empty or oversized in a way that
  breaks commands.

## Risks / Trade-offs

- A long reply becomes several Telegram notifications — accepted; the
  alternative loses content.
- Character-count limit vs Telegram's actual UTF-16 code-unit accounting:
  a 4096-Python-character cap is strictly stricter than 4096 UTF-16 units
  for astral emoji ( surrogate pairs), so no message that passes our check
  can exceed Telegram's limit.

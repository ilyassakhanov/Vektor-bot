# Design

## Context

`bot.handle_message` is the single boundary every text message crosses:
auth gate → document routing → `ConversationManager.handle` →
`Agent.run` → one `reply_to` call. Neither the inbound text nor the outbound
reply is size- or content-checked anywhere today. Telegram's protocol caps
one message at 4096 UTF-16 code units on both sides (inbound text, outbound
send), which makes that boundary the right — and only — place to enforce
safety.

## Goals / Non-Goals

Goals:

- Reject degenerate text input (empty/blank, oversized) with one friendly
  reply and zero side effects (no LLM call, no history mutation).
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
- No validation of document uploads or of the bot-built upload
  notice/caption prompt — documents keep their own flow and gates.
- No env knob for the 4096 limit — it is a Telegram protocol constant, not
  a tuning value; the warn-and-fallback env convention applies to tuning
  knobs, not protocol limits.

## Realization

- `agent/validation.py` — two pure functions, no I/O:
  - `validate_input(text) -> Verdict` — `ok` (scrubbed text) or `reject`
    (reason); rejects raw `len > 4096` (`too_long`), then strips the
    invisible set — zero-width `U+200B`–`U+200D`, `U+2060`, `U+FEFF`, and
    bidi controls `U+202A`–`U+202E`, `U+2066`–`U+2069` — and rejects a
    blank result (`empty`). Nothing else is stripped or trimmed.
  - `split_reply(text, limit=4096) -> list[str]` — ordered chunks, each at
    most `limit` UTF-16 code units (`len(s.encode("utf-16-le")) // 2`);
    split preference paragraph → sentence → word; hard-cut fallback for an
    unbreakable token longer than `limit`; cuts fall on Python `str`
    indices so a surrogate pair is never split; concatenation of chunks
    always equals the input; a text that fits returns `[text]`.
- `bot.py` wiring:
  - `handle_message`: after the auth gate **and after the document
    branch**, on the text path only (immediately before
    `ConversationManager.handle`, inside the same `try` so the principal is
    still reset), validate; a rejection sends one friendly constant reply
    and returns.
  - Both agent-reply send sites (text path, document-handler agent turn)
    send `split_reply(response)` chunks in order via `reply_to` through
    the shared `_send_reply` helper, which skips whitespace-only chunks
    (Telegram rejects them as "message text is empty") and, if nothing is
    left, sends exactly one `_LLM_ERROR_REPLY` fallback.
  - Friendly rejection replies are module constants in the existing
    `_…_REPLY` style.
- Tests: pure-function tests plus handler-level tests with the existing
  `FakeLLM`/`ScriptedLLM` and fake-TeleBot recorders — rejection paths
  assert `llm.chat_calls == []` and untouched conversation history; send
  paths assert per-chunk UTF-16 lengths, order, and that no `parse_mode`
  kwarg is ever recorded.

## Decisions

- Reject oversized input rather than truncate (agreed with maintainer): a
  silently shortened question can lose the actual question (usually at the
  end); an honest ask-to-shorten reply is better UX and cheaper than a
  wrong answer to a beheaded prompt.
- Split oversized output rather than truncate (agreed with maintainer): no
  content is lost; ordered sequential sends are acceptable.
- Validation placement — handler boundary, after the auth gate and after
  the document branch, on the text path only: unauthorized senders are
  answered by the gate and never reach validation; a document message
  (which usually has no `text`) takes the document branch first and is
  never rejected as empty; rejected text messages never enter
  `ConversationManager`, so history stays clean without any change to the
  conversation manager.
- Commands are unaffected: `/documents` and `/delete` are separate TeleBot
  handlers registered before the catch-all; `/new` is non-empty text that
  reaches `ConversationManager` and is matched exactly — validation never
  sees a message it could misclassify as empty or oversized in a way that
  breaks commands.

## Risks / Trade-offs

- A long reply becomes several Telegram notifications — accepted; the
  alternative loses content.
- Telegram counts message length in UTF-16 code units, not Python
  characters. An astral character (most emoji) is 1 Python character but 2
  UTF-16 units, so a 4096-character cap is *looser* than Telegram's limit
  and could still produce an over-limit send. `split_reply` therefore
  measures UTF-16 code units. The inbound check counts Python characters;
  that is acceptable because it is defensive only — Telegram already
  enforces its 4096-unit cap on inbound text upstream.
- `U+200D` (ZERO WIDTH JOINER) is in the strip set, so ZWJ emoji sequences
  (e.g. 👨‍👩‍👧) degrade to their component emoji before reaching the agent.
  Accepted: the visible meaning survives, and keeping ZWJ would leave a
  zero-width character usable to smuggle invisible content into the
  context.

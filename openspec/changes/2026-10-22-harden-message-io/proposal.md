# Proposal

## Why

Today `handle_message` (`bot.py`) forwards `message.text or ""` to the
conversation manager unchanged: an empty message becomes an LLM call with an
empty user turn, and nothing checks message length on the way in. On the way
out, the agent's reply is passed to `bot.reply_to` as a single Telegram
message with no size handling — Telegram's API hard limit is 4096 characters
per message, so any agent answer longer than that (a verbose CVE summary, a
long document answer) fails the send and the user never sees it. Replies are
already sent without `parse_mode`, which is why Telegram markup
metacharacters never break delivery — but that contract is neither specified
nor tested, so a future "improvement" to MarkdownV2 rendering could silently
break every reply.

## What Changes

- Adds input validation at the message-handler boundary: empty or
  whitespace-only messages and messages longer than 4096 characters get
  exactly one friendly reply and never reach the LLM, the document flow, or
  the conversation history.
- Strips zero-width and bidirectional-override control characters from
  accepted input; visible Unicode (emoji, combining marks, non-Latin
  scripts) passes through unchanged.
- Splits outgoing agent replies longer than 4096 characters into an ordered
  sequence of Telegram messages (paragraph, then sentence, then word
  boundaries), on the text-message path and the document-upload turn.
- Specifies plain-text replies as a contract: no `parse_mode` is ever
  requested; markup metacharacters pass through to the user unchanged.

## Capabilities

### Added Capabilities

- `message-io-safety` — inbound message validation (empty/blank, oversized,
  invisible control characters), outbound reply splitting at the Telegram
  4096-character limit, and the plain-text reply contract.

## Impact

- Production: a new pure validation/splitting module (`agent/validation.py`)
  wired into `bot.py` (`handle_message` and the document-handler reply
  path). The agent loop, conversation manager, tools and retrieval stack are
  untouched.
- Tests: new offline suites (`tests/test_input_validation.py`,
  `tests/test_output_safety.py`) built on the existing fakes and fake-TeleBot
  pattern; the default pytest run stays offline and network-free.
- Risk: low — validation is a pure function consulted at the handler
  boundary; splitting changes delivery shape only for replies that
  previously failed to send at all. Commands (`/new`, `/documents`,
  `/delete`) are unaffected: they are non-empty by construction and routed
  before or separately from the validated text path.

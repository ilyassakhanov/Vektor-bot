# message-io-safety Specification

## Purpose
Guard the Telegram message boundary in both directions. Inbound text is
validated at the bot handler before it reaches the agent: empty, blank and
oversized messages get one friendly reply with no LLM call and no history
change, and invisible zero-width and bidi control characters are stripped.
Outbound agent replies are delivered as plain text, split into ordered
chunks within Telegram's 4096 UTF-16 code-unit limit, with no content lost
and no blank message ever sent.

## Requirements

### Requirement: Empty and blank messages are rejected before the agent

A text message that is empty or whitespace-only SHALL be answered with
exactly one friendly reply and SHALL NOT reach the LLM or the conversation
history. Validation SHALL apply only to the text path, after the document
branch; a document message SHALL NOT be rejected for lacking text.

#### Scenario: Empty message gets one friendly reply

- **WHEN** an authorized user sends a message whose text is empty
- **THEN** the user receives exactly one friendly reply and no LLM call is made

#### Scenario: Whitespace-only message is treated as empty

- **WHEN** the message text contains only spaces, tabs or newlines
- **THEN** the message is rejected exactly like an empty one

#### Scenario: Rejected messages never enter history

- **WHEN** a message is rejected as empty or blank
- **THEN** nothing is appended to the chat's conversation history and the next accepted message runs against the prior context unchanged

#### Scenario: Document without text is not rejected

- **WHEN** an authorized user uploads a document with no text and no caption
- **THEN** it is routed to the document flow and no rejection reply is sent

### Requirement: Oversized messages are rejected

A text message longer than 4096 characters (Telegram's message limit) SHALL be
answered with exactly one friendly reply asking the user to shorten it; it
SHALL NOT reach the LLM or the conversation history.
The limit is a Telegram protocol constant and SHALL NOT be configurable.

#### Scenario: Boundary message is accepted

- **WHEN** a message is exactly 4096 characters long
- **THEN** it is processed normally

#### Scenario: Boundary message is rejected

- **WHEN** a message is 4097 characters long
- **THEN** it is rejected with one friendly reply and no LLM call is made

### Requirement: Invisible control characters are stripped

Before an accepted message reaches the agent, zero-width and
bidirectional-override control characters SHALL be stripped. Visible
content — including emoji, combining marks and non-Latin scripts — SHALL
pass through unchanged.

#### Scenario: Zero-width and bidi characters are removed

- **WHEN** an accepted message contains zero-width or bidi-override control characters
- **THEN** the agent receives the message without them

#### Scenario: Visible Unicode is untouched

- **WHEN** an accepted message contains emoji or Cyrillic text
- **THEN** the agent receives that text unchanged

### Requirement: Long replies are split into ordered messages

An agent reply longer than 4096 UTF-16 code units (Telegram's unit) SHALL be
split into ordered messages, each at most 4096 UTF-16 code units, sent
through the same reply path with no content lost. Splits SHALL prefer
paragraph, then sentence, then word boundaries, hard-cutting only a token
longer than the limit. This SHALL apply to document-upload turns too.
Blank chunks SHALL NOT be sent; if none remain, one fallback reply SHALL be.

#### Scenario: A long reply arrives as ordered chunks

- **WHEN** the agent produces a 10,000-character reply
- **THEN** the user receives ordered messages, each at most 4096 UTF-16 code units, whose concatenation equals the reply

#### Scenario: Boundary reply is not split

- **WHEN** the agent produces a reply of exactly 4096 ASCII characters (4096 UTF-16 code units)
- **THEN** it is delivered as a single message

#### Scenario: Astral characters count as two units

- **WHEN** a reply consists of emoji outside the Basic Multilingual Plane and exceeds 4096 UTF-16 code units
- **THEN** every delivered chunk is at most 4096 UTF-16 code units and no surrogate pair is split

#### Scenario: The document-upload turn is covered

- **WHEN** the agent's reply on a document-upload turn exceeds 4096 UTF-16 code units
- **THEN** it is split by the same rule as text-message replies

#### Scenario: Blank chunks are never sent

- **WHEN** a chunk produced by splitting an agent reply is whitespace-only
- **THEN** that chunk is skipped and only the non-blank chunks are delivered, in order

#### Scenario: Blank agent reply gets one fallback

- **WHEN** the agent's reply (on a text or document-upload turn) is empty or whitespace-only, so no chunk is left to send
- **THEN** the user receives exactly one friendly fallback reply instead

### Requirement: Replies are sent as plain text

Outgoing replies SHALL be sent without a `parse_mode`; Telegram markup
metacharacters in agent output SHALL pass through to the user unchanged.

#### Scenario: Markup metacharacters survive delivery

- **WHEN** an agent reply contains `_`, `*`, `[` or other MarkdownV2 metacharacters
- **THEN** the reply is delivered unchanged and no `parse_mode` is requested

### Requirement: Per-user rate limiting

After the auth gate and before the document branch, the handler SHALL admit
each user to at most one in-flight message and at most
`RATE_LIMIT_MAX_MESSAGES` (default 10) per sliding `RATE_LIMIT_WINDOW_S`
seconds (default 60). A rejected message SHALL get exactly one friendly reply,
SHALL NOT reach the LLM or the history, and SHALL be counted in
`vektor_rate_limited_total{reason}`. Uploads, `/new` and messages later
rejected by input validation SHALL each use a slot.

#### Scenario: A concurrent second message gets a busy reply

- **WHEN** a user sends a message while their previous message is still being handled
- **THEN** they receive exactly one busy reply, no LLM call is made, and the conversation history is untouched

#### Scenario: The window limit rejects excess messages

- **WHEN** a user has had `RATE_LIMIT_MAX_MESSAGES` messages accepted within the window and sends another
- **THEN** they receive exactly one rate-limited reply and no LLM call is made

#### Scenario: Uploads are counted

- **WHEN** a window-exhausted user uploads a document
- **THEN** the upload is rejected before the file is downloaded

#### Scenario: New-conversation command is counted

- **WHEN** a user sends `/new`
- **THEN** it consumes a window slot like any other message

#### Scenario: Validation rejects still use a slot

- **WHEN** an admitted message is then rejected as empty or too long
- **THEN** it has still used one window slot

#### Scenario: Users are independent

- **WHEN** one user is rate limited
- **THEN** another user's messages are still admitted

#### Scenario: The slot is released after an agent error

- **WHEN** the agent raises an LLM error while handling a message
- **THEN** the user's next message is admitted

### Requirement: Configurable worker threads

The TeleBot worker thread count SHALL come from `BOT_WORKER_THREADS` (default 4,
at most 64).

#### Scenario: Worker threads are configurable

- **WHEN** `BOT_WORKER_THREADS` is unset, invalid or below 1
- **THEN** the bot uses 4 worker threads and logs a warning for an invalid value

#### Scenario: Worker threads are clamped

- **WHEN** `BOT_WORKER_THREADS` is above 64
- **THEN** the bot uses 64 worker threads and logs a warning

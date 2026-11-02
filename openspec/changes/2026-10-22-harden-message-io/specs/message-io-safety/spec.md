# Delta: message-io-safety

## ADDED Requirements

### Requirement: Empty and blank messages are rejected before the agent

A text message that is empty or whitespace-only SHALL be answered with
exactly one friendly reply and SHALL NOT reach the LLM, the document flow,
or the conversation history.

#### Scenario: Empty message gets one friendly reply

- **WHEN** an authorized user sends a message whose text is empty
- **THEN** the user receives exactly one friendly reply and no LLM call is made

#### Scenario: Whitespace-only message is treated as empty

- **WHEN** the message text contains only spaces, tabs or newlines
- **THEN** the message is rejected exactly like an empty one

#### Scenario: Rejected messages never enter history

- **WHEN** a message is rejected as empty or blank
- **THEN** nothing is appended to the chat's conversation history and the next accepted message runs against the prior context unchanged

### Requirement: Oversized messages are rejected

A message longer than Telegram's 4096-character message limit SHALL be
answered with exactly one friendly reply asking the user to shorten it; it
SHALL NOT reach the LLM, the document flow, or the conversation history.
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

An outgoing agent reply longer than 4096 characters SHALL be split into an
ordered sequence of messages, each at most 4096 characters, delivered
sequentially through the same reply path, with no content lost. Split
points SHALL respect paragraph, then sentence, then word boundaries, with a
hard cut only for a token longer than the limit. The same rule SHALL apply
to the agent's reply on a document-upload turn.

#### Scenario: A long reply arrives as ordered chunks

- **WHEN** the agent produces a 10,000-character reply
- **THEN** the user receives ordered messages, each at most 4096 characters, whose concatenation equals the reply

#### Scenario: Boundary reply is not split

- **WHEN** the agent produces a reply of exactly 4096 characters
- **THEN** it is delivered as a single message

#### Scenario: The document-upload turn is covered

- **WHEN** the agent's reply on a document-upload turn exceeds 4096 characters
- **THEN** it is split by the same rule as text-message replies

### Requirement: Replies are sent as plain text

Outgoing replies SHALL be sent without a `parse_mode`; Telegram markup
metacharacters in agent output SHALL pass through to the user unchanged.

#### Scenario: Markup metacharacters survive delivery

- **WHEN** an agent reply contains `_`, `*`, `[` or other MarkdownV2 metacharacters
- **THEN** the reply is delivered unchanged and no `parse_mode` is requested
